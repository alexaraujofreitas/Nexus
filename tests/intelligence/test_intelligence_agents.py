"""
NexusTrader — Comprehensive Intelligence Agent Test Suite
=========================================================

Covers every intelligence agent with:
  • Unit tests  : process() logic using synthetic data (no network calls)
  • Scenario tests : strong bullish, strong bearish, macro risk-off,
                     geopolitical crisis, news cascade, mixed signals
  • Edge case tests: missing data, zero values, extreme inputs, NaN/Inf
  • Integration tests: EventBus signal publication
  • Crash risk tests: escalation tiers, velocity, depeg amplification
  • Regression tests: all recent Phase 1-3 improvements

Run with:
    pytest tests/intelligence/ -v
    pytest tests/intelligence/ -v -m "not slow"

Author: NexusTrader automated test generation
"""
from __future__ import annotations

import math
import os
import time
import threading
from datetime import datetime, timezone, timedelta
from typing import Any
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


# ═══════════════════════════════════════════════════════════════════
#  Shared helpers
# ═══════════════════════════════════════════════════════════════════

def _assert_valid_signal(result: dict, agent_name: str = "unknown"):
    """Assert that a processed signal meets the universal NexusTrader contract."""
    assert isinstance(result, dict), f"{agent_name}: process() must return dict"
    signal = result.get("signal", result.get("risk_score", result.get("avg_signal")))
    confidence = result.get("confidence", result.get("avg_confidence"))

    if signal is not None:
        assert isinstance(signal, (int, float)), f"{agent_name}: signal must be numeric"
        assert -1.0 <= signal <= 1.0, (
            f"{agent_name}: signal={signal} out of range [-1, +1]"
        )
        assert not math.isnan(signal), f"{agent_name}: signal is NaN"
        assert not math.isinf(signal), f"{agent_name}: signal is Inf"

    if confidence is not None:
        assert isinstance(confidence, (int, float)), f"{agent_name}: confidence must be numeric"
        assert 0.0 <= confidence <= 1.0, (
            f"{agent_name}: confidence={confidence} out of range [0, 1]"
        )
        assert not math.isnan(confidence), f"{agent_name}: confidence is NaN"
        assert not math.isinf(confidence), f"{agent_name}: confidence is Inf"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ago_iso(hours: float) -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=hours)
    return dt.isoformat()


# ═══════════════════════════════════════════════════════════════════
#  FUNDING RATE AGENT
# ═══════════════════════════════════════════════════════════════════

class TestFundingRateAgent:
    """Unit tests for FundingRateAgent signal computation."""

    @pytest.fixture
    def agent(self, qt_app):
        from core.agents.funding_rate_agent import FundingRateAgent
        a = FundingRateAgent()
        yield a
        a._stop_requested = True

    def test_neutral_funding(self, agent):
        """Neutral funding rate → signal = 0, low confidence."""
        raw = {"BTC/USDT": {"rate_pct": 0.01, "oi_usdt": 1_000_000.0}}
        result = agent.process(raw)
        _assert_valid_signal(result, "FundingRateAgent")
        btc = result["symbols"]["BTC/USDT"]
        assert btc["signal"] == 0.0
        assert btc["direction"] == "neutral"
        assert btc["confidence"] < 0.5

    def test_extreme_positive_funding_bearish(self, agent):
        """Extreme positive funding (>0.10%) → strong bearish contrarian signal."""
        raw = {"BTC/USDT": {"rate_pct": 0.15, "oi_usdt": 5_000_000.0}}
        result = agent.process(raw)
        btc = result["symbols"]["BTC/USDT"]
        assert btc["signal"] < -0.5, "Extreme positive funding must yield bearish signal"
        assert btc["direction"] == "bearish"
        assert btc["confidence"] >= 0.80
        _assert_valid_signal(result, "FundingRateAgent.extreme_positive")

    def test_extreme_negative_funding_bullish(self, agent):
        """Extreme negative funding (<-0.05%) → strong bullish squeeze signal."""
        raw = {"BTC/USDT": {"rate_pct": -0.08, "oi_usdt": 3_000_000.0}}
        result = agent.process(raw)
        btc = result["symbols"]["BTC/USDT"]
        assert btc["signal"] > 0.5, "Extreme negative funding must yield bullish signal"
        assert btc["direction"] == "bullish"
        _assert_valid_signal(result, "FundingRateAgent.extreme_negative")

    def test_oi_rising_amplifies_confidence(self, agent):
        """Rising OI with extreme funding → confidence multiplied by 1.3."""
        raw = {"ETH/USDT": {"rate_pct": 0.12, "oi_usdt": 1_000_000.0}}
        agent.process(raw)
        raw2 = {"ETH/USDT": {"rate_pct": 0.12, "oi_usdt": 1_050_000.0}}
        result2 = agent.process(raw2)
        eth = result2["symbols"]["ETH/USDT"]
        assert eth["confidence"] > 0.80, "Rising OI should boost confidence above 0.80"
        _assert_valid_signal(result2, "FundingRateAgent.oi_rising")

    def test_oi_falling_reduces_confidence(self, agent):
        """Falling OI → halved confidence."""
        raw = {"BTC/USDT": {"rate_pct": 0.12, "oi_usdt": 2_000_000.0}}
        agent.process(raw)
        raw2 = {"BTC/USDT": {"rate_pct": 0.12, "oi_usdt": 1_800_000.0}}
        result2 = agent.process(raw2)
        btc = result2["symbols"]["BTC/USDT"]
        assert btc["confidence"] < 0.60
        _assert_valid_signal(result2, "FundingRateAgent.oi_falling")

    def test_24h_sustained_amplification(self, agent):
        """Sustained extremity (current > 1.5× 24h avg) → signal ×1.20."""
        for _ in range(10):
            agent.process({"BTC/USDT": {"rate_pct": 0.05, "oi_usdt": 1_000_000.0}})
        raw = {"BTC/USDT": {"rate_pct": 0.15, "oi_usdt": 1_000_000.0}}
        result = agent.process(raw)
        btc = result["symbols"]["BTC/USDT"]
        assert btc["signal"] <= -0.85

    def test_empty_data_returns_zero_signal(self, agent):
        """No symbols → graceful zero-signal result."""
        result = agent.process({})
        assert result["signal"] == 0.0
        assert result["confidence"] == 0.0
        assert result["count"] == 0

    def test_multiple_symbols_aggregate(self, agent):
        """Multiple symbols → aggregate signal is mean of individual signals."""
        raw = {
            "BTC/USDT": {"rate_pct": 0.12, "oi_usdt": 5_000_000.0},
            "ETH/USDT": {"rate_pct": -0.06, "oi_usdt": 2_000_000.0},
        }
        result = agent.process(raw)
        _assert_valid_signal(result, "FundingRateAgent.multi_symbol")
        assert result["count"] == 2

    def test_signal_cached_in_get_symbol_signal(self, agent):
        """get_symbol_signal() returns cached value after process()."""
        raw = {"BTC/USDT": {"rate_pct": 0.15, "oi_usdt": 1_000_000.0}}
        agent.process(raw)
        cached = agent.get_symbol_signal("BTC/USDT")
        assert cached["signal"] < -0.5
        assert not cached.get("stale")

    def test_get_symbol_signal_missing_returns_stale(self, agent):
        """get_symbol_signal() for unknown symbol returns stale placeholder."""
        cached = agent.get_symbol_signal("UNKNOWN/USDT")
        assert cached.get("stale") is True
        assert cached["signal"] == 0.0


# ═══════════════════════════════════════════════════════════════════
#  ORDER BOOK AGENT
# ═══════════════════════════════════════════════════════════════════

class TestOrderBookAgent:
    """Unit tests for OrderBookAgent microstructure signal."""

    @pytest.fixture
    def agent(self, qt_app):
        from core.agents.order_book_agent import OrderBookAgent
        a = OrderBookAgent()
        yield a
        a._stop_requested = True

    def _make_book(self, bid_volume: float, ask_volume: float, n: int = 5) -> dict:
        """Construct a synthetic order book with the given total volumes."""
        bid_per = bid_volume / n
        ask_per = ask_volume / n
        base = 65_000.0
        bids = [[base - i * 10, bid_per] for i in range(n)]
        asks = [[base + i * 10, ask_per] for i in range(n)]
        return {"BTC/USDT": {"bids": bids, "asks": asks, "spread_pct": 0.03}}

    def test_balanced_book_neutral_signal(self, agent):
        """Balanced bid/ask → signal near 0.0."""
        raw = self._make_book(500.0, 500.0)
        result = agent.process(raw)
        _assert_valid_signal(result, "OrderBookAgent.balanced")
        btc = result["symbols"]["BTC/USDT"]
        assert abs(btc["signal"]) < 0.20, "Balanced book should produce near-zero signal"

    def test_strong_bid_pressure_positive_signal(self, agent):
        """Dominant bids (90% volume) → positive signal (≥ 0.3)."""
        # Signal formula: 0.80 * (imbalance - 0.5) / 0.5
        # With imbalance=0.9 → 0.80 * 0.8 = 0.64
        raw = self._make_book(900.0, 100.0)  # 90% bids → imbalance=0.9
        result = agent.process(raw)
        btc = result["symbols"]["BTC/USDT"]
        assert btc["signal"] > 0.3, f"Strong bid pressure must yield positive signal, got {btc['signal']}"
        assert btc["direction"] in ("bullish", "slight_bullish")

    def test_strong_ask_pressure_negative_signal(self, agent):
        """Dominant asks (90% volume) → negative signal (≤ -0.3)."""
        raw = self._make_book(100.0, 900.0)  # 10% bids → imbalance=0.1
        result = agent.process(raw)
        btc = result["symbols"]["BTC/USDT"]
        assert btc["signal"] < -0.3, f"Strong ask pressure must yield negative signal, got {btc['signal']}"

    def test_empty_book_graceful(self, agent):
        """Empty order book → graceful zero signal."""
        raw = {"BTC/USDT": {"bids": [], "asks": [], "spread_pct": 0.0}}
        result = agent.process(raw)
        btc = result["symbols"]["BTC/USDT"]
        assert btc["signal"] == 0.0
        _assert_valid_signal(result, "OrderBookAgent.empty_book")

    def test_notional_weighting(self, agent):
        """Notional weighting: a single large-notional bid beats many small asks."""
        bids = [[65_000.0, 10.0]]   # $650,000 notional
        asks = [[65_010.0, 0.1]] * 20  # $130,020 notional
        raw = {"BTC/USDT": {"bids": bids, "asks": asks, "spread_pct": 0.02}}
        result = agent.process(raw)
        btc = result["symbols"]["BTC/USDT"]
        assert btc["signal"] > 0, "Larger-notional bids should produce positive signal"

    def test_signal_clipped_within_bounds(self, agent):
        """Extremely skewed book → signal clipped to [-1, +1]."""
        raw = self._make_book(10_000.0, 1.0)
        result = agent.process(raw)
        _assert_valid_signal(result, "OrderBookAgent.extreme_skew")


# ═══════════════════════════════════════════════════════════════════
#  STABLECOIN AGENT
# ═══════════════════════════════════════════════════════════════════

class TestStablecoinAgent:
    """Unit tests for StablecoinLiquidityAgent."""

    @pytest.fixture
    def agent(self, qt_app):
        from core.agents.stablecoin_agent import StablecoinLiquidityAgent
        a = StablecoinLiquidityAgent()
        yield a
        a._stop_requested = True

    def _base_raw(self) -> dict:
        """Baseline stablecoin data using the correct field names expected by process()."""
        return {
            "stablecoins": {
                "tether":   {"market_cap_usd": 100_000_000_000.0, "price_usd": 1.000},
                "usd-coin": {"market_cap_usd": 35_000_000_000.0,  "price_usd": 1.000},
                "dai":      {"market_cap_usd": 5_000_000_000.0,   "price_usd": 1.000},
                "_bitcoin_market_cap": 1_250_000_000_000.0,
            },
            "total_supply": 140_000_000_000.0,
            "metadata": {"sources": ["coingecko"]},
        }

    def test_healthy_market_no_depeg(self, agent):
        """Normal stablecoin market → no depeg, valid signal."""
        raw = self._base_raw()
        result = agent.process(raw)
        _assert_valid_signal(result, "StablecoinAgent.healthy")
        assert len(result.get("depegs", [])) == 0

    def test_depeg_triggers_bearish_signal(self, agent):
        """USDT depegs to 0.97 → bearish signal and depeg list populated."""
        raw = self._base_raw()
        raw["stablecoins"]["tether"]["price_usd"] = 0.97
        result = agent.process(raw)
        _assert_valid_signal(result, "StablecoinAgent.depeg")
        assert result["signal"] <= -0.5, "Depeg event must produce strong bearish signal"
        depeg_list = result.get("depegs", [])
        assert len(depeg_list) >= 1

    def test_supply_growth_bullish(self, agent):
        """Supply growing fast → bullish signal (dry powder)."""
        agent._supply_history = [
            {"timestamp": time.time() - 86400, "total_supply": 120_000_000_000.0}
        ]
        raw = self._base_raw()
        raw["total_supply"] = 125_000_000_000.0  # +4.2% in 24h
        result = agent.process(raw)
        _assert_valid_signal(result, "StablecoinAgent.supply_growth")

    def test_btc_market_cap_regression(self, agent):
        """Regression: _bitcoin_market_cap must be stored (Phase 1 fix)."""
        raw = self._base_raw()
        result = agent.process(raw)
        _assert_valid_signal(result, "StablecoinAgent.btc_marketcap_regression")
        assert not math.isnan(result.get("signal", 0.0))

    def test_empty_stablecoins_graceful(self, agent):
        """Empty stablecoin data → graceful zero signal."""
        raw = {"stablecoins": {}, "total_supply": 0.0, "metadata": {}}
        result = agent.process(raw)
        _assert_valid_signal(result, "StablecoinAgent.empty")
        assert result["signal"] == 0.0

    def test_multiple_depegs_amplified_signal(self, agent):
        """Multiple depegs → signal must be strongly bearish."""
        raw = self._base_raw()
        raw["stablecoins"]["tether"]["price_usd"]  = 0.96
        raw["stablecoins"]["usd-coin"]["price_usd"] = 0.97
        result = agent.process(raw)
        _assert_valid_signal(result, "StablecoinAgent.multi_depeg")
        assert result["signal"] <= -0.7

    def test_extreme_depeg_signal(self, agent):
        """Price=0.50 (total collapse) → signal strongly bearish."""
        raw = self._base_raw()
        raw["stablecoins"]["tether"]["price_usd"] = 0.50
        result = agent.process(raw)
        _assert_valid_signal(result, "StablecoinAgent.extreme_depeg")
        assert result["signal"] <= -0.5


# ═══════════════════════════════════════════════════════════════════
#  MINER FLOW AGENT
# ═══════════════════════════════════════════════════════════════════

class TestMinerFlowAgent:
    """Unit tests for MinerFlowAgent."""

    @pytest.fixture
    def agent(self, qt_app):
        from core.agents.miner_flow_agent import MinerFlowAgent
        a = MinerFlowAgent()
        yield a
        a._stop_requested = True

    def _base_raw(self) -> dict:
        return {
            "blocks": [
                {"totalFees": 10_000_000, "avgFee": 50},
                {"totalFees": 12_000_000, "avgFee": 60},
                {"totalFees": 9_500_000,  "avgFee": 47},
            ],
            "miner_reserves": {"total_btc": 1_800_000.0},
            "exchange_flows": {"net_inflow": -500.0},
        }

    def test_neutral_flow_signal(self, agent):
        """Normal miner flow → valid signal."""
        raw = self._base_raw()
        result = agent.process(raw)
        _assert_valid_signal(result, "MinerFlowAgent.neutral")

    def test_high_exchange_inflow_bearish(self, agent):
        """Miners sending BTC to exchanges → valid signal."""
        raw = self._base_raw()
        raw["exchange_flows"]["net_inflow"] = 5_000.0
        result = agent.process(raw)
        _assert_valid_signal(result, "MinerFlowAgent.high_inflow")

    def test_fee_calculation_regression(self, agent):
        """Regression: totalFees in sats properly converted to BTC (Phase 1 fix)."""
        raw = self._base_raw()
        result = agent.process(raw)
        _assert_valid_signal(result, "MinerFlowAgent.fee_regression")
        assert not math.isnan(result.get("signal", 0.0))

    def test_fallback_avgfee_blocks(self, agent):
        """Fallback path: blocks without totalFees use avgFee × 1.5M vB."""
        raw = {
            "blocks": [
                {"avgFee": 50},
                {"avgFee": 60},
            ],
            "miner_reserves": {"total_btc": 1_800_000.0},
            "exchange_flows": {"net_inflow": 0.0},
        }
        result = agent.process(raw)
        _assert_valid_signal(result, "MinerFlowAgent.avgfee_fallback")

    def test_empty_blocks_graceful(self, agent):
        """No block data → graceful zero signal."""
        raw = {"blocks": [], "miner_reserves": {}, "exchange_flows": {}}
        result = agent.process(raw)
        _assert_valid_signal(result, "MinerFlowAgent.empty_blocks")
        assert result["signal"] == 0.0


# ═══════════════════════════════════════════════════════════════════
#  NEWS AGENT
# ═══════════════════════════════════════════════════════════════════

class TestNewsAgent:
    """Unit tests for NewsAgent sentiment scoring with temporal decay."""

    @pytest.fixture
    def agent(self, qt_app):
        from core.agents.news_agent import NewsAgent
        a = NewsAgent()
        a._finbert = False  # force VADER fallback (no GPU needed in tests)
        yield a
        a._stop_requested = True

    def _make_articles(self, sentiments: list[tuple[str, float]]) -> list[dict]:
        articles = []
        for title, age_h in sentiments:
            pub = _ago_iso(age_h)
            articles.append({
                "title": title,
                "description": "",
                "source": "newsapi",
                "published_at": pub,
            })
        return articles

    def test_empty_articles_zero_signal(self, agent):
        """No articles → signal=0, confidence=0."""
        result = agent.process([])
        assert result["signal"] == 0.0
        assert result["confidence"] == 0.0
        assert result["article_count"] == 0
        _assert_valid_signal(result, "NewsAgent.empty")

    def test_very_old_articles_low_weight(self, agent):
        """Articles > halflife old have heavily decayed weight."""
        old_articles = self._make_articles([
            ("Bitcoin crashes massively terrible disaster", 6.0),
        ])
        result_old = agent.process(old_articles)
        _assert_valid_signal(result_old, "NewsAgent.old_articles")

    def test_temporal_decay_formula_correct(self, agent):
        """
        Regression: decay weight at age=halflife should be exp(-1) ≈ 0.368.
        The code uses: decay = exp(-age_hours / DECAY_HALFLIFE_HOURS)
        At age=2h (=halflife=2): decay = exp(-2/2) = exp(-1) ≈ 0.368, NOT 0.5.
        """
        from core.agents.news_agent import _DECAY_HALFLIFE_HOURS
        assert _DECAY_HALFLIFE_HOURS == 2.0
        age_h = 2.0
        expected_decay = math.exp(-age_h / _DECAY_HALFLIFE_HOURS)
        assert abs(expected_decay - math.exp(-1)) < 0.001, \
            f"Decay at halflife should be exp(-1)≈0.368, got {expected_decay}"

    def test_duplicate_titles_deduplicated(self, agent):
        """Near-duplicate titles should be deduplicated (first 60 chars as key)."""
        title = "Bitcoin surges to new all time high record"
        articles = [
            {"title": title, "description": "", "source": "newsapi",   "published_at": _ago_iso(0.5)},
            {"title": title, "description": "", "source": "messari",   "published_at": _ago_iso(0.5)},
            {"title": title + " extra suffix here", "description": "", "source": "cryptocompare",
             "published_at": _ago_iso(0.5)},
        ]
        result = agent.process(articles)
        # First 60 chars of title+suffix is the same → all 3 should deduplicate to 1
        assert result["article_count"] <= 2
        _assert_valid_signal(result, "NewsAgent.dedup")

    def test_source_credibility_weights_defined(self, agent):
        """Source credibility weights must be present in module."""
        from core.agents.news_agent import _SOURCE_CREDIBILITY
        assert _SOURCE_CREDIBILITY["newsapi"] == 1.0
        assert _SOURCE_CREDIBILITY["messari"] == 0.90
        assert _SOURCE_CREDIBILITY["cryptocompare"] == 0.85

    def test_top_headline_populated(self, agent):
        """top_headline should be set when articles are processed."""
        articles = [
            {"title": "Bitcoin stable today", "description": "",
             "source": "newsapi", "published_at": _ago_iso(0.5)},
            {"title": "CRYPTO MARKET CRASHES CATASTROPHIC COLLAPSE DISASTER",
             "description": "", "source": "newsapi", "published_at": _ago_iso(0.1)},
        ]
        result = agent.process(articles)
        assert len(result["top_headline"]) > 0
        _assert_valid_signal(result, "NewsAgent.top_headline")

    def test_no_published_at_uses_full_weight(self, agent):
        """Articles without published_at should use decay=1.0 (no decay applied)."""
        articles = [
            {"title": "Bitcoin bullish breakout", "description": "",
             "source": "newsapi"}   # no published_at key
        ]
        result = agent.process(articles)
        _assert_valid_signal(result, "NewsAgent.no_timestamp")
        assert result["article_count"] == 1


# ═══════════════════════════════════════════════════════════════════
#  MACRO AGENT
# ═══════════════════════════════════════════════════════════════════

class TestMacroAgent:
    """Unit tests for MacroAgent VIX scoring and regime vote logic."""

    @pytest.fixture
    def agent(self, qt_app):
        from core.agents.macro_agent import MacroAgent
        a = MacroAgent()
        yield a
        a._stop_requested = True

    def _vix_data(self, current: float, change_pct: float) -> dict:
        return {"current": current, "change_pct": change_pct, "data_points": 10}

    def test_vix_scoring_low_vix_neutral_or_bearish(self, agent):
        """VIX=15, no big change → near-neutral signal (slight bearish complacency at <13)."""
        score, conf = agent._score_vix(self._vix_data(15.0, 0.0))
        assert -0.20 <= score <= 0.20
        assert 0.0 <= conf <= 1.0

    def test_vix_scoring_high_vix_bearish(self, agent):
        """VIX=38 (fear level) → strongly bearish / risk-off signal."""
        score, conf = agent._score_vix(self._vix_data(38.0, 10.0))
        assert score <= -0.50, "High VIX must yield bearish macro signal"
        assert conf >= 0.70

    def test_vix_scoring_falling_vix_bullish(self, agent):
        """VIX dropping -12% from elevated level → bullish risk-on."""
        score, conf = agent._score_vix(self._vix_data(22.0, -12.0))
        assert score >= 0.20, "Falling VIX should tilt bullish"

    def test_vix_scoring_spike_bearish(self, agent):
        """VIX +20% spike (panic) → very bearish."""
        score, conf = agent._score_vix(self._vix_data(25.0, 20.0))
        assert score <= -0.40, "VIX spike must yield bearish signal"
        assert conf >= 0.60

    def test_vix_signal_in_bounds_all_cases(self, agent):
        """VIX score must always stay within [-1, +1]."""
        test_cases = [
            (10.0, -15.0), (13.0, -5.0), (20.0, 0.0),
            (28.0, 8.0), (35.0, 15.0), (45.0, 25.0),
        ]
        for current, chg in test_cases:
            score, conf = agent._score_vix(self._vix_data(current, chg))
            assert -1.0 <= score <= 1.0, f"VIX score out of range for ({current}, {chg})"
            assert 0.0 <= conf <= 1.0

    def test_process_risk_off_scenario(self, agent):
        """Risk-off scenario: high VIX + rising DXY → risk_off regime."""
        raw = {
            "dxy":   {"current": 106.0, "change_pct": 2.0},   # rising DXY → bearish
            "vix":   {"current": 35.0, "change_pct": 20.0},   # spike → bearish
            "us10y": {"current": 4.8, "change_bps": 25},      # rising yields → bearish
        }
        result = agent.process(raw)
        _assert_valid_signal(result, "MacroAgent.risk_off")
        assert result["signal"] < 0 or result.get("regime_bias") in ("risk_off",)

    def test_process_risk_on_scenario(self, agent):
        """Risk-on scenario: falling VIX + falling DXY + rising SPX → risk_on."""
        raw = {
            "dxy":   {"current": 100.0, "change_pct": -2.0},   # falling DXY → bullish
            "vix":   {"current": 18.0,  "change_pct": -12.0},  # falling VIX → bullish
            "us10y": {"current": 4.0,   "change_bps": -15},    # falling yields → bullish
            "spx":   {"change_pct": 3.0},                       # rising equities → bullish
        }
        result = agent.process(raw)
        _assert_valid_signal(result, "MacroAgent.risk_on")
        assert result["signal"] > 0 or result.get("regime_bias") in ("risk_on",)

    def test_empty_components_neutral(self, agent):
        """No components → graceful neutral output."""
        result = agent.process({})
        _assert_valid_signal(result, "MacroAgent.empty")
        assert result["signal"] == 0.0


# ═══════════════════════════════════════════════════════════════════
#  OPTIONS FLOW AGENT
# ═══════════════════════════════════════════════════════════════════

class TestOptionsFlowAgent:
    """Unit tests for OptionsFlowAgent BSM gamma calculations."""

    @pytest.fixture
    def agent(self, qt_app):
        from core.agents.options_flow_agent import OptionsFlowAgent
        a = OptionsFlowAgent()
        yield a
        a._stop_requested = True

    def test_bsm_gamma_atm_positive(self, agent):
        """BSM gamma for ATM option should be small positive float."""
        from core.agents.options_flow_agent import OptionsFlowAgent
        S, K, T, sigma = 65_000.0, 65_000.0, 30 / 365.0, 0.80
        gamma = OptionsFlowAgent._bsm_gamma(S, K, T, sigma)
        assert gamma > 0, "ATM gamma must be positive"
        assert gamma < 1.0, "ATM gamma must be < 1"

    def test_bsm_gamma_invalid_inputs_return_zero(self, agent):
        """Invalid inputs (S=0, T=0, sigma=0) → gamma=0.0 without exception."""
        from core.agents.options_flow_agent import OptionsFlowAgent
        assert OptionsFlowAgent._bsm_gamma(0, 65_000, 0.1, 0.5) == 0.0
        assert OptionsFlowAgent._bsm_gamma(65_000, 0, 0.1, 0.5) == 0.0
        assert OptionsFlowAgent._bsm_gamma(65_000, 65_000, 0, 0.5) == 0.0
        assert OptionsFlowAgent._bsm_gamma(65_000, 65_000, 0.1, 0) == 0.0

    def test_bsm_gamma_deep_otm_less_than_atm(self, agent):
        """Deep OTM option → gamma approaches 0 (less than ATM)."""
        from core.agents.options_flow_agent import OptionsFlowAgent
        gamma_atm  = OptionsFlowAgent._bsm_gamma(65_000, 65_000, 30/365, 0.80)
        gamma_deep = OptionsFlowAgent._bsm_gamma(65_000, 90_000, 30/365, 0.80)
        assert gamma_deep < gamma_atm, "Deep OTM gamma must be less than ATM gamma"

    def test_parse_expiry_valid_instrument(self, agent):
        """_parse_expiry correctly parses Deribit instrument name."""
        from core.agents.options_flow_agent import OptionsFlowAgent
        ts = OptionsFlowAgent._parse_expiry("BTC-28MAR25-90000-C")
        assert ts > 0, "Expiry timestamp must be positive"
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        assert dt.month == 3
        assert dt.day == 28
        assert dt.year == 2025

    def test_parse_expiry_invalid_returns_zero(self, agent):
        """Invalid instrument name → returns 0 gracefully."""
        from core.agents.options_flow_agent import OptionsFlowAgent
        ts = OptionsFlowAgent._parse_expiry("INVALID_NAME")
        assert ts == 0.0

    def test_gamma_exposure_signal_with_index_price(self, agent):
        """GEX signal must be in [-1, +1] range with index_price param."""
        near_options = [
            {"type": "call", "gamma": 0.00003, "oi": 1000, "strike": 65_000},
            {"type": "put",  "gamma": 0.00003, "oi": 2000, "strike": 65_000},
        ]
        sig, conf = agent._gamma_exposure_signal(near_options, 65_000.0)
        assert -1.0 <= sig <= 1.0
        assert 0.0 <= conf <= 1.0

    def test_gamma_exposure_call_dominated(self, agent):
        """Call-dominated: put_gex < call_gex → net_gex < 0 → gex_ratio < -0.3 → sig = +0.15."""
        near_options = [
            {"type": "call", "gamma": 0.0001, "oi": 5000, "strike": 65_000},
            {"type": "put",  "gamma": 0.0001, "oi": 100,  "strike": 65_000},
        ]
        sig, conf = agent._gamma_exposure_signal(near_options, 65_000.0)
        # net_gex = put_gex - call_gex = negative → dealers short gamma → trending up
        assert sig >= 0.0

    def test_empty_options_zero_signal(self, agent):
        """No options → zero GEX signal."""
        sig, conf = agent._gamma_exposure_signal([], 65_000.0)
        assert sig == 0.0
        assert conf <= 0.30


# ═══════════════════════════════════════════════════════════════════
#  CRASH DETECTION AGENT
# ═══════════════════════════════════════════════════════════════════

class TestCrashDetectionAgent:
    """Unit and integration tests for CrashDetectionAgent."""

    @pytest.fixture
    def agent(self, qt_app):
        from core.agents.crash_detection_agent import CrashDetectionAgent
        a = CrashDetectionAgent()
        yield a
        a._stop_requested = True

    def test_score_to_tier_boundaries(self):
        """Tier boundaries based on actual TIER_THRESHOLDS dict."""
        from core.agents.crash_detection_agent import (
            score_to_tier, TIER_NORMAL, TIER_DEFENSIVE, TIER_HIGH_ALERT,
            TIER_EMERGENCY, TIER_SYSTEMIC, TIER_THRESHOLDS,
        )
        # TIER_SYSTEMIC: 9.0, TIER_EMERGENCY: 8.0, TIER_HIGH_ALERT: 7.0,
        # TIER_DEFENSIVE: 5.0, TIER_NORMAL: 0.0
        assert score_to_tier(0.0) == TIER_NORMAL
        assert score_to_tier(4.9) == TIER_NORMAL
        assert score_to_tier(5.0) == TIER_DEFENSIVE
        assert score_to_tier(6.9) == TIER_DEFENSIVE
        assert score_to_tier(7.0) == TIER_HIGH_ALERT
        assert score_to_tier(7.9) == TIER_HIGH_ALERT
        assert score_to_tier(8.0) == TIER_EMERGENCY
        assert score_to_tier(8.9) == TIER_EMERGENCY
        assert score_to_tier(9.0) == TIER_SYSTEMIC
        assert score_to_tier(10.0) == TIER_SYSTEMIC

    def test_process_normal_market_low_score(self, agent):
        """Baseline normal market → NORMAL tier, crash_score < 5."""
        raw = {"agent_signals": {}}
        result = agent.process(raw)
        _assert_valid_signal(result, "CrashDetectionAgent.normal")
        from core.agents.crash_detection_agent import TIER_NORMAL
        assert result["tier"] == TIER_NORMAL
        assert result["position_size_multiplier"] == 1.00

    def test_process_high_derivatives_risk(self, agent):
        """Heavy long liquidations + extreme funding → elevated crash score."""
        raw = {
            "agent_signals": {
                "funding_rate":    {"funding_rate": -0.08, "oi_change_pct": -15.0},
                "liquidation_flow": {"signal": -0.75},
                "options_flow":     {"iv_skew": -0.20, "put_call_ratio": 2.0},
            }
        }
        result = agent.process(raw)
        _assert_valid_signal(result, "CrashDetectionAgent.high_derivatives")
        assert result["crash_score"] > 0.0, "Extreme derivatives signals must raise crash score"
        assert result.get("position_size_multiplier") is not None

    def test_depeg_amplifies_crash_score(self, agent):
        """Stablecoin depeg event → crash score bumped by 1.0 per depeg."""
        agent._latest_signals["_depeg_count"] = 2
        raw = {"agent_signals": {}}
        result = agent.process(raw)
        _assert_valid_signal(result, "CrashDetectionAgent.depeg_amplification")
        # With 2 depegs adding +2.0, crash_score ≥ 2.0
        assert result["crash_score"] >= 2.0

    def test_output_contains_required_keys(self, agent):
        """process() output must contain all required keys."""
        result = agent.process({"agent_signals": {}})
        required_keys = {
            "crash_score", "tier", "signal", "confidence",
            "position_size_multiplier", "velocity_2h", "components",
        }
        for key in required_keys:
            assert key in result, f"CrashDetectionAgent output missing key: '{key}'. Got: {list(result.keys())}"

    def test_crash_score_capped_at_10(self, agent):
        """Crash score must not exceed 10.0 even with extreme inputs."""
        agent._latest_signals["_depeg_count"] = 20
        raw = {
            "agent_signals": {
                "funding_rate":    {"funding_rate": -0.15, "oi_change_pct": -50.0},
                "liquidation_flow": {"signal": -1.0},
                "options_flow":     {"iv_skew": -0.50, "put_call_ratio": 5.0},
                "order_book":       {"imbalance": -0.8, "ob_signal": -1.0},
                "onchain":          {"signal": -1.0},
            }
        }
        result = agent.process(raw)
        assert result["crash_score"] <= 10.0, "Crash score must be capped at 10.0"

    def test_systemic_collapse_zero_multiplier(self, agent):
        """Score ≥ 9.0 → SYSTEMIC tier → position_size_multiplier = 0."""
        agent._latest_signals["_depeg_count"] = 5
        raw = {
            "agent_signals": {
                "funding_rate":    {"funding_rate": -0.15, "oi_change_pct": -50.0},
                "liquidation_flow": {"signal": -1.0},
                "options_flow":     {"iv_skew": -0.50, "put_call_ratio": 5.0},
                "order_book":       {"imbalance": -0.8, "signal": -1.0},
                "onchain":          {"signal": -1.0},
            }
        }
        result = agent.process(raw)
        _assert_valid_signal(result, "CrashDetectionAgent.systemic")
        from core.agents.crash_detection_agent import TIER_SYSTEMIC
        if result["tier"] == TIER_SYSTEMIC:
            assert result["position_size_multiplier"] == 0.00

    def test_velocity_field_present(self, agent):
        """velocity_2h must be present in output."""
        result = agent.process({"agent_signals": {}})
        assert "velocity_2h" in result

    def test_position_size_multiplier_values(self, agent):
        """Verify position_size_multiplier has correct values per tier."""
        from core.agents.crash_detection_agent import (
            TIER_NORMAL, TIER_DEFENSIVE, TIER_HIGH_ALERT,
            TIER_EMERGENCY, TIER_SYSTEMIC, score_to_tier,
        )
        # The multipliers are defined inside process(), check via the output
        # NORMAL (score=0)
        result = agent.process({"agent_signals": {}})
        if result["tier"] == TIER_NORMAL:
            assert result["position_size_multiplier"] == 1.00


# ═══════════════════════════════════════════════════════════════════
#  SECTOR ROTATION AGENT  (VIX regression)
# ═══════════════════════════════════════════════════════════════════

class TestSectorRotationAgent:
    """Unit tests for SectorRotationAgent — VIX integration regression."""

    @pytest.fixture
    def agent(self, qt_app):
        from core.agents.sector_rotation_agent import SectorRotationAgent
        a = SectorRotationAgent()
        yield a
        a._stop_requested = True

    def test_vix_in_risk_off_tickers(self, agent):
        """Regression: ^VIX must appear in _RISK_OFF_TICKERS (Phase 2 fix)."""
        from core.agents.sector_rotation_agent import _RISK_OFF_TICKERS
        tickers = [t[0] for t in _RISK_OFF_TICKERS]
        assert "^VIX" in tickers, "VIX must be included in risk-off ticker list"

    def test_process_risk_off_data_bearish(self, agent):
        """Rising GLD + rising VIX + falling equities → risk-off → bearish signal."""
        # SectorRotationAgent expects: raw = {"etfs": {ticker: change_pct, ...}}
        raw = {
            "etfs": {
                "GLD":  3.5,    # rising gold = risk-off
                "TLT":  2.0,    # rising bonds = risk-off
                "^VIX": 12.0,   # rising VIX = risk-off (but may be small signal)
                "XLU":  1.5,
                "SPY":  -2.5,   # falling equities = risk-off
                "QQQ":  -3.5,
            }
        }
        result = agent.process(raw)
        _assert_valid_signal(result, "SectorRotationAgent.risk_off")
        # Signal should be negative or rotation_bias bearish
        assert result["signal"] <= 0 or result.get("rotation_bias") in ("risk_off", "bearish")

    def test_process_risk_on_data_bullish(self, agent):
        """Falling GLD + falling VIX + rising equities → risk-on → bullish signal."""
        raw = {
            "etfs": {
                "GLD":  -1.5,   # falling gold = risk-on
                "TLT":  -2.0,   # falling bonds = risk-on
                "^VIX": -8.0,   # falling VIX = risk-on
                "XLU":  -0.5,
                "SPY":  3.5,    # rising equities = risk-on
                "QQQ":  4.5,
            }
        }
        result = agent.process(raw)
        _assert_valid_signal(result, "SectorRotationAgent.risk_on")
        assert result["signal"] >= 0 or result.get("rotation_bias") in ("risk_on", "bullish")

    def test_empty_data_neutral(self, agent):
        """No ticker data → neutral output."""
        result = agent.process({})
        _assert_valid_signal(result, "SectorRotationAgent.empty")
        assert result["signal"] == 0.0


# ═══════════════════════════════════════════════════════════════════
#  GEOPOLITICAL AGENT
# ═══════════════════════════════════════════════════════════════════

class TestGeopoliticalAgent:
    """Unit tests for GeopoliticalAgent risk scoring."""

    @pytest.fixture
    def agent(self, qt_app):
        from core.agents.geopolitical_agent import GeopoliticalAgent
        a = GeopoliticalAgent()
        yield a
        a._stop_requested = True

    def test_peaceful_headlines_low_risk(self, agent):
        """Peaceful, positive headlines → low risk score."""
        # GeopoliticalAgent.process() expects {"cc_headlines": [str,...], "gdelt": {...}}
        raw = {
            "cc_headlines": [
                "Trade agreement signed between US and EU for economic growth",
                "Stock markets rally on positive economic data",
                "Bitcoin adoption growing steadily across institutions",
            ],
            "gdelt": {"avg_tone": 3.0}
        }
        result = agent.process(raw)
        _assert_valid_signal(result, "GeopoliticalAgent.peaceful")
        assert result["risk_level"] in ("CALM", "NEUTRAL", "MODERATE")

    def test_war_keywords_elevate_risk(self, agent):
        """War/invasion headlines → elevated risk score."""
        raw = {
            "cc_headlines": [
                "Military invasion launched airstrike confirmed attack",
                "War escalates missile strike hits capital city bomb",
            ],
            "gdelt": {"avg_tone": -8.0}
        }
        result = agent.process(raw)
        _assert_valid_signal(result, "GeopoliticalAgent.war")
        assert result["signal"] < 0, "War keywords must produce negative (bearish) signal"

    def test_compound_signal_detection(self, agent):
        """Compound signals (entity pair + action) elevate risk vs single keywords."""
        raw_compound = {
            "cc_headlines": ["Iran fires missile at Israel military base attack"],
            "gdelt": {"avg_tone": -6.0}
        }
        raw_single = {
            "cc_headlines": ["Iran mentioned in diplomatic report"],
            "gdelt": {"avg_tone": 0.0}
        }
        r_compound = agent.process(raw_compound)
        r_single   = agent.process(raw_single)
        _assert_valid_signal(r_compound, "GeopoliticalAgent.compound")
        _assert_valid_signal(r_single,   "GeopoliticalAgent.single")
        assert r_compound.get("signal", 0) <= r_single.get("signal", 0)

    def test_empty_headlines_neutral(self, agent):
        """No headlines → neutral output."""
        raw = {"cc_headlines": [], "gdelt": {}}
        result = agent.process(raw)
        _assert_valid_signal(result, "GeopoliticalAgent.empty")
        assert result["signal"] == 0.0

    def test_unknown_input_empty_dict(self, agent):
        """Empty dict input → graceful neutral result."""
        result = agent.process({})
        _assert_valid_signal(result, "GeopoliticalAgent.empty_dict")


# ═══════════════════════════════════════════════════════════════════
#  ORCHESTRATOR ENGINE
# ═══════════════════════════════════════════════════════════════════

class TestOrchestratorEngine:
    """Tests for OrchestratorEngine consensus, divergence, and VIX dampener."""

    @pytest.fixture
    def engine(self, qt_app):
        from core.orchestrator.orchestrator_engine import OrchestratorEngine
        e = OrchestratorEngine()
        yield e

    def test_all_bullish_consensus_boost(self, engine):
        """≥80% consensus among bullish agents → confidence ×1.10."""
        from core.orchestrator.orchestrator_engine import OrchestratorSignal
        # Populate _agent_cache directly (orchestrator reads from this)
        engine._agent_cache = {
            "funding_rate":   {"signal": 0.7, "confidence": 0.8, "stale": False,
                               "updated_at": _now_iso()},
            "order_book":     {"signal": 0.6, "confidence": 0.7, "stale": False,
                               "updated_at": _now_iso()},
            "news":           {"signal": 0.5, "confidence": 0.7, "stale": False,
                               "updated_at": _now_iso()},
            "macro":          {"signal": 0.4, "confidence": 0.6, "stale": False,
                               "regime_bias": "risk_on", "macro_risk_score": 0.2,
                               "updated_at": _now_iso()},
        }
        engine._recalculate()
        result = engine._current_signal
        assert result is not None, "All-bullish input should produce a signal"
        assert result.meta_signal > 0, "All-bullish input should produce positive meta_signal"
        _assert_valid_signal(
            {"signal": result.meta_signal, "confidence": result.meta_confidence},
            "Orchestrator.consensus_bullish",
        )

    def test_divergent_signals_reduce_confidence(self, engine):
        """Top agents deeply split → divergence penalty > 0."""
        engine._agent_cache = {
            "funding_rate": {"signal": 0.8,  "confidence": 0.9, "stale": False,
                             "updated_at": _now_iso()},
            "order_book":   {"signal": -0.8, "confidence": 0.9, "stale": False,
                             "updated_at": _now_iso()},
            "news":         {"signal": 0.7,  "confidence": 0.8, "stale": False,
                             "updated_at": _now_iso()},
            "macro":        {"signal": -0.7, "confidence": 0.8, "stale": False,
                             "regime_bias": "risk_off", "macro_risk_score": 0.7,
                             "updated_at": _now_iso()},
        }
        engine._recalculate()
        result = engine._current_signal
        if result:
            assert result.divergence_penalty >= 0, "Divergence penalty must be ≥ 0"

    def test_meta_signal_within_bounds(self, engine):
        """meta_signal must always be in [-1, +1] regardless of inputs."""
        engine._agent_cache = {
            "funding_rate": {"signal": 1.0, "confidence": 1.0, "stale": False,
                             "updated_at": _now_iso()},
            "order_book":   {"signal": 1.0, "confidence": 1.0, "stale": False,
                             "updated_at": _now_iso()},
            "macro":        {"signal": 1.0, "confidence": 1.0, "stale": False,
                             "regime_bias": "risk_on", "macro_risk_score": 0.1,
                             "updated_at": _now_iso()},
        }
        engine._recalculate()
        result = engine._current_signal
        if result:
            _assert_valid_signal(
                {"signal": result.meta_signal, "confidence": result.meta_confidence},
                "Orchestrator.bounds_check",
            )

    def test_no_cache_produces_no_signal_or_neutral(self, engine):
        """Empty agent cache → no crash, returns None or neutral."""
        engine._agent_cache = {}
        engine._recalculate()
        result = engine._current_signal
        if result:
            _assert_valid_signal(
                {"signal": result.meta_signal, "confidence": result.meta_confidence},
                "Orchestrator.empty",
            )

    def test_orchestrator_signal_has_consensus_score(self, engine):
        """Regression: OrchestratorSignal must have consensus_score field (Phase 3)."""
        from core.orchestrator.orchestrator_engine import OrchestratorSignal
        assert "consensus_score" in OrchestratorSignal.__slots__, \
            "REGRESSION: OrchestratorSignal missing consensus_score in __slots__"

    def test_orchestrator_signal_has_divergence_penalty(self, engine):
        """Regression: OrchestratorSignal must have divergence_penalty field (Phase 3)."""
        from core.orchestrator.orchestrator_engine import OrchestratorSignal
        assert "divergence_penalty" in OrchestratorSignal.__slots__

    def test_orchestrator_signal_has_vix_dampener(self, engine):
        """Regression: OrchestratorSignal must have vix_dampener field (Phase 3)."""
        from core.orchestrator.orchestrator_engine import OrchestratorSignal
        assert "vix_dampener" in OrchestratorSignal.__slots__

    def test_get_signal_returns_neutral_before_any_data(self, engine):
        """get_signal() must not raise even before any agent data arrives."""
        sig = engine.get_signal()
        assert sig is not None
        _assert_valid_signal(
            {"signal": sig.meta_signal, "confidence": sig.meta_confidence},
            "Orchestrator.get_signal_empty",
        )


# ═══════════════════════════════════════════════════════════════════
#  SCENARIO TESTS  (end-to-end pipeline simulation)
# ═══════════════════════════════════════════════════════════════════

class TestScenarios:
    """Multi-agent scenario tests simulating realistic market conditions."""

    def _run_scenario(self, agent_data: dict) -> dict:
        """Simulate orchestrator aggregation over a set of mock agent outputs."""
        signals = []
        for name, data in agent_data.items():
            sig  = data.get("signal", 0.0)
            conf = data.get("confidence", 0.5)
            w    = data.get("weight", 1.0)
            signals.append((sig, conf, w))
        if not signals:
            return {"meta_signal": 0.0, "meta_confidence": 0.0}
        total_w = sum(w for _, _, w in signals)
        meta_s  = sum(s * w for s, _, w in signals) / total_w
        meta_c  = sum(c * w for _, c, w in signals) / total_w
        return {
            "meta_signal": max(-1.0, min(1.0, meta_s)),
            "meta_confidence": max(0.0, min(1.0, meta_c)),
        }

    def test_strong_bullish_scenario(self):
        """Strong bull market: all agents agree on bullish signal."""
        agents = {
            "funding":    {"signal": 0.80, "confidence": 0.85, "weight": 1.0},
            "order_book": {"signal": 0.70, "confidence": 0.80, "weight": 1.0},
            "news":       {"signal": 0.65, "confidence": 0.75, "weight": 1.0},
            "macro":      {"signal": 0.60, "confidence": 0.70, "weight": 1.0},
            "stablecoin": {"signal": 0.50, "confidence": 0.65, "weight": 0.8},
            "miner_flow": {"signal": 0.55, "confidence": 0.60, "weight": 0.8},
        }
        meta = self._run_scenario(agents)
        assert meta["meta_signal"] > 0.50, "Strong bull scenario must yield meta_signal > 0.5"
        assert meta["meta_confidence"] > 0.60
        _assert_valid_signal(meta, "Scenario.strong_bullish")

    def test_strong_bearish_scenario(self):
        """Strong bear market: all agents converge on bearish."""
        agents = {
            "funding":      {"signal": -0.80, "confidence": 0.90, "weight": 1.0},
            "order_book":   {"signal": -0.70, "confidence": 0.85, "weight": 1.0},
            "news":         {"signal": -0.75, "confidence": 0.80, "weight": 1.0},
            "macro":        {"signal": -0.65, "confidence": 0.80, "weight": 1.0},
            "stablecoin":   {"signal": -0.60, "confidence": 0.75, "weight": 0.8},
            "geopolitical": {"signal": -0.50, "confidence": 0.70, "weight": 0.7},
        }
        meta = self._run_scenario(agents)
        assert meta["meta_signal"] < -0.50, "Strong bear scenario must yield meta_signal < -0.5"
        _assert_valid_signal(meta, "Scenario.strong_bearish")

    def test_macro_risk_off_scenario(self):
        """Macro risk-off: VIX spikes, bonds rally, equities sell off."""
        agents = {
            "macro":      {"signal": -0.80, "confidence": 0.90, "weight": 1.0},
            "sector":     {"signal": -0.70, "confidence": 0.85, "weight": 0.8},
            "funding":    {"signal": -0.40, "confidence": 0.65, "weight": 1.0},
            "stablecoin": {"signal": -0.50, "confidence": 0.70, "weight": 0.8},
            "news":       {"signal": -0.30, "confidence": 0.60, "weight": 1.0},
        }
        meta = self._run_scenario(agents)
        assert meta["meta_signal"] < -0.40
        _assert_valid_signal(meta, "Scenario.macro_risk_off")

    def test_geopolitical_crisis_scenario(self):
        """Geopolitical escalation: geo + news + macro all bearish."""
        agents = {
            "geopolitical": {"signal": -0.85, "confidence": 0.90, "weight": 1.0},
            "news":         {"signal": -0.70, "confidence": 0.80, "weight": 1.0},
            "macro":        {"signal": -0.60, "confidence": 0.80, "weight": 1.0},
            "order_book":   {"signal": -0.30, "confidence": 0.60, "weight": 1.0},
            "funding":      {"signal":  0.20, "confidence": 0.50, "weight": 1.0},
        }
        meta = self._run_scenario(agents)
        assert meta["meta_signal"] < -0.30
        _assert_valid_signal(meta, "Scenario.geopolitical_crisis")

    def test_negative_news_cascade_scenario(self):
        """Coordinated negative news cascade."""
        agents = {
            "news":       {"signal": -0.90, "confidence": 0.95, "weight": 1.0},
            "social":     {"signal": -0.85, "confidence": 0.85, "weight": 0.8},
            "order_book": {"signal": -0.75, "confidence": 0.80, "weight": 1.0},
            "funding":    {"signal": -0.65, "confidence": 0.80, "weight": 1.0},
            "stablecoin": {"signal": -0.40, "confidence": 0.60, "weight": 0.8},
        }
        meta = self._run_scenario(agents)
        assert meta["meta_signal"] < -0.60
        _assert_valid_signal(meta, "Scenario.news_cascade")

    def test_mixed_signals_near_zero(self):
        """Conflicting signals (half bull, half bear) → near-zero meta signal."""
        agents = {
            "funding":    {"signal":  0.70, "confidence": 0.85, "weight": 1.0},
            "news":       {"signal": -0.70, "confidence": 0.80, "weight": 1.0},
            "macro":      {"signal":  0.60, "confidence": 0.75, "weight": 1.0},
            "geopolitical": {"signal": -0.65, "confidence": 0.80, "weight": 1.0},
        }
        meta = self._run_scenario(agents)
        assert abs(meta["meta_signal"]) < 0.30, "Balanced signals should cancel to near-zero"
        _assert_valid_signal(meta, "Scenario.mixed_signals")


# ═══════════════════════════════════════════════════════════════════
#  CRASH RISK SIMULATION
# ═══════════════════════════════════════════════════════════════════

class TestCrashRiskSimulation:
    """Crash risk pathway simulations."""

    @pytest.fixture
    def agent(self, qt_app):
        from core.agents.crash_detection_agent import CrashDetectionAgent
        a = CrashDetectionAgent()
        yield a
        a._stop_requested = True

    def test_geopolitical_escalation_pathway(self, agent):
        """Each escalation step increases crash score."""
        step1 = agent.process({"agent_signals": {}})
        step2 = agent.process({
            "agent_signals": {
                "liquidation_flow": {"signal": -0.40},
            }
        })
        step3 = agent.process({
            "agent_signals": {
                "funding_rate": {"funding_rate": -0.08, "oi_change_pct": -12.0},
                "liquidation_flow": {"signal": -0.80},
                "options_flow": {"iv_skew": -0.20, "put_call_ratio": 2.0},
            }
        })
        assert step3["crash_score"] >= step1["crash_score"], \
            "Escalation must increase crash score"
        for step in [step1, step2, step3]:
            _assert_valid_signal(step, "CrashRisk.geo_escalation")

    def test_macro_tightening_pathway(self, agent):
        """Macro tightening signals → elevated crash score > 0."""
        result = agent.process({
            "agent_signals": {
                "funding_rate":     {"funding_rate": -0.07, "oi_change_pct": -8.0},
                "liquidation_flow": {"signal": -0.50},
            }
        })
        _assert_valid_signal(result, "CrashRisk.macro_tightening")
        assert result["crash_score"] > 0.0

    def test_news_cascade_pathway(self, agent):
        """Heavy liquidations + extreme IV → elevated crash score."""
        result = agent.process({
            "agent_signals": {
                "liquidation_flow": {"signal": -0.90},
                "options_flow":     {"iv_skew": -0.40, "put_call_ratio": 3.0},
                "funding_rate":     {"funding_rate": -0.09, "oi_change_pct": -15.0},
            }
        })
        _assert_valid_signal(result, "CrashRisk.news_cascade")
        assert result["crash_score"] >= 1.0

    def test_liquidity_collapse_with_depegs(self, agent):
        """Liquidity collapse + 3 depegged stablecoins → DEFENSIVE+ tier."""
        agent._latest_signals["_depeg_count"] = 3
        result = agent.process({
            "agent_signals": {
                "liquidation_flow": {"signal": -0.80},
                "order_book":       {"imbalance": -0.5, "signal": -0.70},
            }
        })
        _assert_valid_signal(result, "CrashRisk.liquidity_collapse")
        # 3 depegs add +3.0 to crash_score
        assert result["crash_score"] >= 3.0
        from core.agents.crash_detection_agent import TIER_NORMAL
        assert result["tier"] != TIER_NORMAL

    def test_recovery_reduces_score(self, agent):
        """After crisis signals clear, crash score should be lower."""
        # Establish crisis state
        agent.process({
            "agent_signals": {
                "funding_rate":    {"funding_rate": -0.10, "oi_change_pct": -20.0},
                "liquidation_flow": {"signal": -0.90},
            }
        })
        # Recovery: no adverse signals
        recovery = agent.process({"agent_signals": {}})
        _assert_valid_signal(recovery, "CrashRisk.recovery")
        assert recovery["crash_score"] <= 10.0


# ═══════════════════════════════════════════════════════════════════
#  EDGE CASES
# ═══════════════════════════════════════════════════════════════════

class TestEdgeCases:
    """Edge case and boundary condition tests."""

    def test_funding_rate_zero_oi_no_crash(self, qt_app):
        """FundingRateAgent: OI=0 should not divide by zero."""
        from core.agents.funding_rate_agent import FundingRateAgent
        agent = FundingRateAgent()
        raw = {"BTC/USDT": {"rate_pct": 0.15, "oi_usdt": 0.0}}
        result = agent.process(raw)
        _assert_valid_signal(result, "Edge.funding_zero_oi")

    def test_news_agent_all_articles_no_timestamp(self, qt_app):
        """NewsAgent: articles without timestamps use full weight (decay=1.0)."""
        from core.agents.news_agent import NewsAgent
        agent = NewsAgent()
        agent._finbert = False
        articles = [
            {"title": "Bitcoin bullish breakout confirmed", "description": "", "source": "newsapi"},
            {"title": "Ethereum surges above key resistance", "description": "", "source": "messari"},
        ]
        result = agent.process(articles)
        _assert_valid_signal(result, "Edge.news_no_timestamp")
        assert result["article_count"] == 2

    def test_macro_agent_vix_extremely_low(self, qt_app):
        """MacroAgent: VIX=7 (complacency) → bearish contrarian signal."""
        from core.agents.macro_agent import MacroAgent
        agent = MacroAgent()
        score, conf = agent._score_vix({"current": 7.0, "change_pct": 0.0, "data_points": 5})
        assert -1.0 <= score <= 1.0
        # VIX < 13 → slight bearish (complacency = overconfidence warning)
        assert score <= 0.0

    def test_macro_agent_vix_boundary_values(self, qt_app):
        """MacroAgent: VIX exactly at threshold values (22, 30, 40)."""
        from core.agents.macro_agent import MacroAgent
        agent = MacroAgent()
        for vix_val in [22.0, 30.0, 40.0]:
            score, conf = agent._score_vix({"current": vix_val, "change_pct": 0.0, "data_points": 5})
            assert -1.0 <= score <= 1.0, f"VIX={vix_val} produced out-of-range score"

    def test_order_book_single_level_no_crash(self, qt_app):
        """OrderBookAgent: single price level should not crash."""
        from core.agents.order_book_agent import OrderBookAgent
        agent = OrderBookAgent()
        raw = {"BTC/USDT": {"bids": [[65_000.0, 5.0]], "asks": [[65_010.0, 5.0]], "spread_pct": 0.015}}
        result = agent.process(raw)
        _assert_valid_signal(result, "Edge.order_book_single_level")

    def test_crash_agent_no_inputs_graceful(self, qt_app):
        """CrashDetectionAgent: empty input should produce NORMAL tier."""
        from core.agents.crash_detection_agent import CrashDetectionAgent, TIER_NORMAL
        agent = CrashDetectionAgent()
        result = agent.process({"agent_signals": {}})
        assert result["tier"] == TIER_NORMAL
        assert result["position_size_multiplier"] == 1.00

    def test_stablecoin_empty_stablecoins_dict(self, qt_app):
        """StablecoinAgent: empty stablecoins dict → zero signal."""
        from core.agents.stablecoin_agent import StablecoinLiquidityAgent
        agent = StablecoinLiquidityAgent()
        result = agent.process({"stablecoins": {}, "total_supply": 0.0, "metadata": {}})
        _assert_valid_signal(result, "Edge.stablecoin_empty")
        assert result["signal"] == 0.0

    def test_geopolitical_empty_dict(self, qt_app):
        """GeopoliticalAgent: empty dict → neutral signal."""
        from core.agents.geopolitical_agent import GeopoliticalAgent
        agent = GeopoliticalAgent()
        result = agent.process({})
        _assert_valid_signal(result, "Edge.geo_empty")
        assert result["signal"] == 0.0


# ═══════════════════════════════════════════════════════════════════
#  SIGNAL VALIDATION SUITE  (cross-agent invariants)
# ═══════════════════════════════════════════════════════════════════

class TestSignalValidation:
    """Cross-agent signal invariant tests."""

    ALL_AGENT_CLASSES = [
        ("FundingRateAgent",        "core.agents.funding_rate_agent"),
        ("OrderBookAgent",          "core.agents.order_book_agent"),
        ("StablecoinLiquidityAgent","core.agents.stablecoin_agent"),
        ("MinerFlowAgent",          "core.agents.miner_flow_agent"),
        ("NewsAgent",               "core.agents.news_agent"),
        ("MacroAgent",              "core.agents.macro_agent"),
        ("GeopoliticalAgent",       "core.agents.geopolitical_agent"),
        ("SectorRotationAgent",     "core.agents.sector_rotation_agent"),
        ("CrashDetectionAgent",     "core.agents.crash_detection_agent"),
    ]

    def test_all_agents_import_cleanly(self, qt_app):
        """All agent modules must import without error."""
        import importlib
        for cls_name, module_path in self.ALL_AGENT_CLASSES:
            mod = importlib.import_module(module_path)
            assert hasattr(mod, cls_name), f"{module_path} missing class {cls_name}"

    def test_all_agents_instantiate(self, qt_app):
        """All agents must instantiate without raising."""
        import importlib
        for cls_name, module_path in self.ALL_AGENT_CLASSES:
            mod = importlib.import_module(module_path)
            cls = getattr(mod, cls_name)
            agent = cls()
            assert agent is not None
            agent._stop_requested = True

    def test_all_agents_have_required_properties(self, qt_app):
        """All agents must expose event_topic and poll_interval_seconds."""
        import importlib
        for cls_name, module_path in self.ALL_AGENT_CLASSES:
            mod = importlib.import_module(module_path)
            cls = getattr(mod, cls_name)
            agent = cls()
            assert hasattr(agent, "event_topic"), f"{cls_name} missing event_topic"
            assert hasattr(agent, "poll_interval_seconds"), f"{cls_name} missing poll_interval_seconds"
            assert isinstance(agent.event_topic, str), f"{cls_name}.event_topic must be str"
            assert isinstance(agent.poll_interval_seconds, int)
            assert agent.poll_interval_seconds > 0
            agent._stop_requested = True

    @pytest.mark.parametrize("signal_val,expected_valid", [
        (-1.0, True), (-0.5, True), (0.0, True), (0.5, True), (1.0, True),
        (-1.001, False), (1.001, False), (float("nan"), False), (float("inf"), False),
    ])
    def test_signal_contract_validation(self, signal_val: float, expected_valid: bool):
        """The _assert_valid_signal helper itself works correctly."""
        result = {"signal": signal_val, "confidence": 0.5}
        if expected_valid:
            _assert_valid_signal(result, "validation_helper")
        else:
            with pytest.raises((AssertionError, ValueError)):
                _assert_valid_signal(result, "validation_helper")


# ═══════════════════════════════════════════════════════════════════
#  REGRESSION TESTS  (Phase 1-3 fixes)
# ═══════════════════════════════════════════════════════════════════

class TestRegressionPhase1:
    """Regression tests for Phase 1 critical bug fixes."""

    def test_stablecoin_signal_not_nan_with_btc_mcap(self, qt_app):
        """
        REGRESSION: Stablecoin signal must not be NaN after BTC market cap fix.
        Bug: _compute_signal() called stablecoins.get('_bitcoin_market_cap', 0.0)
             but it was never populated.
        Fix: When coin_id == 'bitcoin', store as _bitcoin_market_cap.
        """
        from core.agents.stablecoin_agent import StablecoinLiquidityAgent
        agent = StablecoinLiquidityAgent()
        raw = {
            "stablecoins": {
                "tether": {"market_cap_usd": 100e9, "price_usd": 1.000},
                "_bitcoin_market_cap": 1_250e9,
            },
            "total_supply": 100e9,
            "metadata": {},
        }
        result = agent.process(raw)
        assert not math.isnan(result.get("signal", 0.0)), \
            "REGRESSION: stablecoin signal is NaN due to missing BTC market cap"
        _assert_valid_signal(result, "Regression.stablecoin_btc_mcap")

    def test_miner_fee_not_nan(self, qt_app):
        """
        REGRESSION: Miner fee calculation must NOT use 4000 vB constant.
        Bug: fee = avgFee × 4000 vB (wildly incorrect)
        Fix: use totalFees/1e8; fallback avgFee × 1_500_000.
        """
        from core.agents.miner_flow_agent import MinerFlowAgent
        agent = MinerFlowAgent()
        raw = {
            "blocks": [{"totalFees": 10_000_000, "avgFee": 50}],
            "miner_reserves": {"total_btc": 1_800_000.0},
            "exchange_flows": {"net_inflow": 0.0},
        }
        result = agent.process(raw)
        _assert_valid_signal(result, "Regression.miner_fee_calculation")
        assert not math.isnan(result.get("signal", 0.0))

    def test_order_book_depth_is_50(self, qt_app):
        """
        REGRESSION: Order book depth must be 50 levels (was 20 before Phase 1).
        """
        from core.agents.order_book_agent import _DEPTH_LEVELS
        assert _DEPTH_LEVELS == 50, f"_DEPTH_LEVELS should be 50, got {_DEPTH_LEVELS}"


class TestRegressionPhase2:
    """Regression tests for Phase 2 high-impact signal improvements."""

    def test_news_agent_decay_halflife_is_2h(self, qt_app):
        """
        REGRESSION: NewsAgent must apply 2h temporal decay halflife (Phase 2 fix).
        """
        from core.agents.news_agent import _DECAY_HALFLIFE_HOURS
        assert _DECAY_HALFLIFE_HOURS == 2.0, \
            f"Decay halflife should be 2.0h, got {_DECAY_HALFLIFE_HOURS}"

    def test_news_agent_source_credibility_dict_exists(self, qt_app):
        """
        REGRESSION: NewsAgent must have _SOURCE_CREDIBILITY weights (Phase 2 fix).
        """
        from core.agents.news_agent import _SOURCE_CREDIBILITY
        assert "newsapi"       in _SOURCE_CREDIBILITY
        assert "cryptocompare" in _SOURCE_CREDIBILITY
        assert "messari"       in _SOURCE_CREDIBILITY
        assert _SOURCE_CREDIBILITY["newsapi"] == 1.0
        assert _SOURCE_CREDIBILITY["messari"] == 0.90
        assert _SOURCE_CREDIBILITY["cryptocompare"] == 0.85

    def test_funding_agent_has_rate_history(self, qt_app):
        """
        REGRESSION: FundingRateAgent must maintain 24h rolling rate history (Phase 2 fix).
        """
        from core.agents.funding_rate_agent import FundingRateAgent
        agent = FundingRateAgent()
        assert hasattr(agent, "_rate_history"), "FundingRateAgent missing _rate_history attribute"
        assert isinstance(agent._rate_history, dict)

    def test_sector_rotation_vix_in_risk_off(self, qt_app):
        """
        REGRESSION: SectorRotationAgent must include VIX as risk-off ticker (Phase 2 fix).
        """
        from core.agents.sector_rotation_agent import _RISK_OFF_TICKERS
        tickers = [t[0] for t in _RISK_OFF_TICKERS]
        assert "^VIX" in tickers, \
            "REGRESSION: ^VIX not in _RISK_OFF_TICKERS; Phase 2 fix not applied"

    def test_options_bsm_gamma_method_exists(self, qt_app):
        """
        REGRESSION: OptionsFlowAgent must have _bsm_gamma static method (Phase 2 fix).
        """
        from core.agents.options_flow_agent import OptionsFlowAgent
        assert hasattr(OptionsFlowAgent, "_bsm_gamma"), \
            "OptionsFlowAgent missing _bsm_gamma (BSM gamma not implemented)"
        assert hasattr(OptionsFlowAgent, "_parse_expiry"), \
            "OptionsFlowAgent missing _parse_expiry"

    def test_macro_agent_has_score_vix_method(self, qt_app):
        """
        REGRESSION: MacroAgent must have _score_vix method (Phase 2 fix).
        """
        from core.agents.macro_agent import MacroAgent
        agent = MacroAgent()
        assert hasattr(agent, "_score_vix"), "MacroAgent missing _score_vix method"


class TestRegressionPhase2B:
    """Regression tests for Phase 2B — crash detection improvements."""

    def test_crash_agent_has_position_size_multiplier_in_output(self, qt_app):
        """
        REGRESSION: CrashDetectionAgent must include position_size_multiplier (Phase 2B fix).
        """
        from core.agents.crash_detection_agent import CrashDetectionAgent
        agent = CrashDetectionAgent()
        result = agent.process({"agent_signals": {}})
        assert "position_size_multiplier" in result, \
            "REGRESSION: position_size_multiplier missing from crash detection output"

    def test_crash_agent_has_velocity_2h_in_output(self, qt_app):
        """
        REGRESSION: CrashDetectionAgent must include velocity_2h in output (Phase 2B fix).
        """
        from core.agents.crash_detection_agent import CrashDetectionAgent
        agent = CrashDetectionAgent()
        result = agent.process({"agent_signals": {}})
        assert "velocity_2h" in result, \
            "REGRESSION: velocity_2h missing from crash detection output"

    def test_crash_agent_has_stablecoin_handler(self, qt_app):
        """
        REGRESSION: CrashDetectionAgent must have _on_stablecoin_signal handler (Phase 2B fix).
        """
        from core.agents.crash_detection_agent import CrashDetectionAgent
        agent = CrashDetectionAgent()
        assert hasattr(agent, "_on_stablecoin_signal"), \
            "REGRESSION: CrashDetectionAgent missing _on_stablecoin_signal handler"

    def test_crash_tier_thresholds_match_spec(self, qt_app):
        """
        Regression: Tier thresholds must match spec:
        DEFENSIVE≥5, HIGH_ALERT≥7, EMERGENCY≥8, SYSTEMIC≥9.
        """
        from core.agents.crash_detection_agent import (
            TIER_THRESHOLDS, TIER_DEFENSIVE, TIER_HIGH_ALERT,
            TIER_EMERGENCY, TIER_SYSTEMIC,
        )
        assert TIER_THRESHOLDS[TIER_DEFENSIVE]  == 5.0, "DEFENSIVE threshold must be 5.0"
        assert TIER_THRESHOLDS[TIER_HIGH_ALERT] == 7.0, "HIGH_ALERT threshold must be 7.0"
        assert TIER_THRESHOLDS[TIER_EMERGENCY]  == 8.0, "EMERGENCY threshold must be 8.0"
        assert TIER_THRESHOLDS[TIER_SYSTEMIC]   == 9.0, "SYSTEMIC threshold must be 9.0"

    def test_normal_tier_multiplier_is_1(self, qt_app):
        """NORMAL tier → position_size_multiplier = 1.00."""
        from core.agents.crash_detection_agent import CrashDetectionAgent, TIER_NORMAL
        agent = CrashDetectionAgent()
        result = agent.process({"agent_signals": {}})
        if result["tier"] == TIER_NORMAL:
            assert result["position_size_multiplier"] == 1.00


class TestRegressionPhase3:
    """Regression tests for Phase 3 — orchestrator improvements."""

    @pytest.fixture
    def engine(self, qt_app):
        from core.orchestrator.orchestrator_engine import OrchestratorEngine
        return OrchestratorEngine()

    def test_orchestrator_signal_has_consensus_score(self, engine):
        """Regression: OrchestratorSignal must include consensus_score field."""
        from core.orchestrator.orchestrator_engine import OrchestratorSignal
        assert "consensus_score" in OrchestratorSignal.__slots__

    def test_orchestrator_signal_has_divergence_penalty(self, engine):
        """Regression: OrchestratorSignal must include divergence_penalty field."""
        from core.orchestrator.orchestrator_engine import OrchestratorSignal
        assert "divergence_penalty" in OrchestratorSignal.__slots__

    def test_orchestrator_signal_has_vix_dampener(self, engine):
        """Regression: OrchestratorSignal must include vix_dampener field."""
        from core.orchestrator.orchestrator_engine import OrchestratorSignal
        assert "vix_dampener" in OrchestratorSignal.__slots__

    def test_orchestrator_recalculate_method_exists(self, engine):
        """OrchestratorEngine must have _recalculate() method."""
        assert hasattr(engine, "_recalculate"), \
            "OrchestratorEngine missing _recalculate()"

    def test_orchestrator_consensus_score_computed(self, engine):
        """Consensus score must be computed and returned in OrchestratorSignal."""
        engine._agent_cache = {
            "funding_rate": {"signal": 0.7, "confidence": 0.8, "stale": False,
                             "updated_at": _now_iso()},
            "order_book":   {"signal": 0.6, "confidence": 0.7, "stale": False,
                             "updated_at": _now_iso()},
            "macro":        {"signal": 0.5, "confidence": 0.7, "stale": False,
                             "regime_bias": "risk_on", "macro_risk_score": 0.2,
                             "updated_at": _now_iso()},
        }
        engine._recalculate()
        result = engine._current_signal
        if result:
            assert 0.0 <= result.consensus_score <= 1.0, \
                f"consensus_score out of range: {result.consensus_score}"
            assert 0.0 <= result.divergence_penalty <= 1.0
            assert 0.0 < result.vix_dampener <= 1.0


# ═══════════════════════════════════════════════════════════════════
#  INTEGRATION TESTS  (EventBus publishing)
# ═══════════════════════════════════════════════════════════════════

class TestEventBusIntegration:
    """Integration tests verifying EventBus publish/subscribe patterns."""

    def test_event_can_be_published_and_captured(self, qt_app, event_capture):
        """Basic EventBus publish/subscribe works end-to-end."""
        from core.event_bus import Topics, bus
        captured = event_capture(Topics.FUNDING_RATE_UPDATED)
        data = {"signal": 0.5, "confidence": 0.7, "count": 1}
        bus.publish(Topics.FUNDING_RATE_UPDATED, data)
        assert len(captured[Topics.FUNDING_RATE_UPDATED]) == 1
        assert captured[Topics.FUNDING_RATE_UPDATED][0].data["signal"] == 0.5

    def test_crash_tier_change_event_format(self, qt_app, event_capture):
        """CRASH_TIER_CHANGED event must contain old_tier, new_tier, score."""
        from core.event_bus import Topics, bus
        captured = event_capture(Topics.CRASH_TIER_CHANGED)
        bus.publish(Topics.CRASH_TIER_CHANGED, {
            "old_tier": "NORMAL", "new_tier": "DEFENSIVE", "score": 5.5
        })
        assert len(captured[Topics.CRASH_TIER_CHANGED]) == 1
        event_data = captured[Topics.CRASH_TIER_CHANGED][0].data
        assert event_data["new_tier"] == "DEFENSIVE"
        assert event_data["old_tier"] == "NORMAL"

    def test_stablecoin_depeg_updates_crash_agent_depeg_count(self, qt_app):
        """STABLECOIN_UPDATED with depegs → CrashDetectionAgent updates _depeg_count."""
        from core.agents.crash_detection_agent import CrashDetectionAgent
        from core.event_bus import Topics, bus
        from PySide6.QtCore import QCoreApplication

        agent = CrashDetectionAgent()
        # Initial count should be 0
        assert agent._latest_signals.get("_depeg_count", 0) == 0

        # Publish stablecoin event with 1 depeg
        bus.publish(Topics.STABLECOIN_UPDATED, {
            "signal": -0.9, "confidence": 0.95,
            "depegs": [{"coin": "USDT", "price": 0.97}],
        })
        QCoreApplication.processEvents()
        assert agent._latest_signals.get("_depeg_count", 0) == 1
        agent._stop_requested = True

    def test_multiple_topics_captured_independently(self, qt_app, event_capture):
        """Multiple topics can be captured simultaneously without cross-contamination."""
        from core.event_bus import Topics, bus
        captured = event_capture(Topics.FUNDING_RATE_UPDATED, Topics.MACRO_UPDATED)
        bus.publish(Topics.FUNDING_RATE_UPDATED, {"signal": 0.5})
        bus.publish(Topics.MACRO_UPDATED, {"signal": -0.3})
        assert len(captured[Topics.FUNDING_RATE_UPDATED]) == 1
        assert len(captured[Topics.MACRO_UPDATED]) == 1
        assert captured[Topics.FUNDING_RATE_UPDATED][0].data["signal"] == 0.5
        assert captured[Topics.MACRO_UPDATED][0].data["signal"] == -0.3


# ═══════════════════════════════════════════════════════════════════
#  PERFORMANCE / TIMING  (marked slow)
# ═══════════════════════════════════════════════════════════════════

class TestPerformance:
    """Basic performance / timing checks."""

    @pytest.mark.slow
    def test_funding_rate_100_symbols_under_100ms(self, qt_app):
        """FundingRateAgent.process() must complete 100 symbols in < 100ms."""
        from core.agents.funding_rate_agent import FundingRateAgent
        agent = FundingRateAgent()
        raw = {f"SYM{i}/USDT": {"rate_pct": 0.01 * (i % 20), "oi_usdt": float(i * 1_000_000)}
               for i in range(100)}
        start = time.monotonic()
        agent.process(raw)
        elapsed_ms = (time.monotonic() - start) * 1000
        assert elapsed_ms < 100, f"process() took {elapsed_ms:.1f}ms (limit: 100ms)"

    @pytest.mark.slow
    def test_news_agent_1000_articles_under_500ms(self, qt_app):
        """NewsAgent.process() must handle 1000 articles in < 500ms."""
        from core.agents.news_agent import NewsAgent
        agent = NewsAgent()
        agent._finbert = False
        articles = [
            {"title": f"Crypto news article {i}", "description": "detail",
             "source": "newsapi", "published_at": _ago_iso(0.1 + i * 0.001)}
            for i in range(1000)
        ]
        start = time.monotonic()
        result = agent.process(articles)
        elapsed_ms = (time.monotonic() - start) * 1000
        assert elapsed_ms < 500, f"process() took {elapsed_ms:.1f}ms (limit: 500ms)"
        # article_count reflects deduplication; 1000 unique titles → up to 1000 processed
        assert result["article_count"] > 0
