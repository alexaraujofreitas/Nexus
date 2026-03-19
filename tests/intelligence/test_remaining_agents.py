"""
NexusTrader — Remaining Intelligence Agent Test Suite
======================================================

Covers the seven agents not included in the original suite:
  • OnChainAgent
  • VolatilitySurfaceAgent
  • WhaleTrackingAgent
  • LiquidationFlowAgent
  • NarrativeShiftAgent
  • SocialSentimentAgent
  • RedditSentimentAgent

All tests use synthetic data and mock any external calls (network, ML models).
No real API keys or GPU required.

Run with:
    pytest tests/intelligence/test_remaining_agents.py -v
    pytest tests/intelligence/ -v   # combined with original suite
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


# ═══════════════════════════════════════════════════════════════════
#  Shared helpers  (mirrored from test_intelligence_agents.py)
# ═══════════════════════════════════════════════════════════════════

def _assert_valid_signal(result: dict, agent_name: str = "unknown"):
    """Assert that a processed signal meets the universal NexusTrader contract."""
    assert isinstance(result, dict), f"{agent_name}: process() must return dict"
    signal = result.get("signal", result.get("avg_signal"))
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


def _unix_now() -> int:
    return int(datetime.now(timezone.utc).timestamp())


# ═══════════════════════════════════════════════════════════════════
#  ON-CHAIN AGENT
# ═══════════════════════════════════════════════════════════════════

class TestOnChainAgent:
    """
    Unit tests for OnChainAgent._compute_signal() and process().

    Signal logic (from source):
      • price_change_24h > 5.0 AND volume_ratio > 1.3  → -0.65 (exchange inflow)
      • price_change_24h < -3.0 AND volume_ratio > 1.5  → +0.50 (capitulation)
      • -2 <= price_change_24h <= 2 AND volume_ratio < 0.8 → +0.35 (accumulation)
      • 2 < price_change_24h <= 5 AND volume_ratio < 1.0  → +0.10 (mild bullish)
      • Mempool / blockchain components weight-blend if keys present
    """

    @pytest.fixture
    def agent(self, qt_app):
        from core.agents.onchain_agent import OnChainAgent
        a = OnChainAgent()
        yield a
        a._stop_requested = True

    # ── empty input ──────────────────────────────────────────

    def test_empty_raw_returns_neutral(self, agent):
        """Empty raw dict → neutral 0.0 signal, 0.0 confidence."""
        result = agent.process({})
        _assert_valid_signal(result, "OnChainAgent")
        assert result["signal"] == 0.0
        assert result["confidence"] == 0.0
        assert result["count"] == 0

    # ── bearish scenario: exchange inflow ────────────────────

    def test_exchange_inflow_bearish(self, agent):
        """
        Large +8% price move with 2× normal volume → exchange inflow detected.
        Expect: signal < 0 (bearish), confidence ≥ 0.4.
        """
        raw = {
            "bitcoin": {
                "price_change_pct_24h": 8.0,
                "price_change_pct_7d":  10.0,
                "volume_24h":           2_000_000_000.0,
                "avg_volume_30d":       1_000_000_000.0,   # volume_ratio = 2.0
            }
        }
        result = agent.process(raw)
        _assert_valid_signal(result, "OnChainAgent")
        assert result["signal"] < 0, (
            f"Expected bearish inflow signal, got {result['signal']}"
        )
        assert result["confidence"] >= 0.4

    # ── bullish scenario: capitulation ──────────────────────

    def test_capitulation_bullish(self, agent):
        """
        -5% price crash with high relative volume → capitulation / buy signal.
        Expect: signal > 0 (bullish), confidence ≥ 0.4.
        """
        raw = {
            "bitcoin": {
                "price_change_pct_24h": -5.0,
                "price_change_pct_7d":  -8.0,
                "volume_24h":           3_000_000_000.0,
                "avg_volume_30d":       1_000_000_000.0,   # volume_ratio = 3.0
            }
        }
        result = agent.process(raw)
        _assert_valid_signal(result, "OnChainAgent")
        assert result["signal"] > 0, (
            f"Expected bullish capitulation signal, got {result['signal']}"
        )

    # ── bullish scenario: accumulation ──────────────────────

    def test_accumulation_bullish(self, agent):
        """
        Flat price, low volume → quiet accumulation.
        Expect: signal > 0, direction contains 'bullish'.
        """
        raw = {
            "bitcoin": {
                "price_change_pct_24h": 0.5,
                "price_change_pct_7d":  1.0,
                "volume_24h":           500_000_000.0,
                "avg_volume_30d":       1_000_000_000.0,   # volume_ratio = 0.5
            }
        }
        result = agent.process(raw)
        _assert_valid_signal(result, "OnChainAgent")
        assert result["signal"] > 0, (
            f"Expected bullish accumulation signal, got {result['signal']}"
        )
        sym_data = result["symbols"]["bitcoin"]
        assert "bullish" in sym_data["direction"].lower()

    # ── multiple symbols ────────────────────────────────────

    def test_multiple_symbols_averaged(self, agent):
        """
        Mix of bearish (BTC inflow) + bullish (ETH accumulation) should
        partially cancel, producing a moderate aggregate.
        """
        raw = {
            "bitcoin": {
                "price_change_pct_24h": 8.0,
                "price_change_pct_7d":  9.0,
                "volume_24h":   2_000_000_000.0,
                "avg_volume_30d": 1_000_000_000.0,
            },
            "ethereum": {
                "price_change_pct_24h": 0.3,
                "price_change_pct_7d":  0.5,
                "volume_24h":   400_000_000.0,
                "avg_volume_30d": 1_000_000_000.0,
            },
        }
        result = agent.process(raw)
        _assert_valid_signal(result, "OnChainAgent")
        assert result["count"] == 2
        assert "bitcoin" in result["symbols"]
        assert "ethereum" in result["symbols"]

    # ── optional mempool / blockchain keys ──────────────────

    def test_missing_optional_fields_no_crash(self, agent):
        """Omitting mempool/blockchain dicts must not crash process()."""
        raw = {
            "bitcoin": {
                "price_change_pct_24h": 1.0,
                "price_change_pct_7d":  2.0,
                "volume_24h":           800_000_000.0,
                "avg_volume_30d":       800_000_000.0,
            }
        }
        result = agent.process(raw)
        _assert_valid_signal(result, "OnChainAgent")

    # ── output structure ────────────────────────────────────

    def test_output_structure(self, agent):
        """process() must return the documented output keys."""
        raw = {
            "bitcoin": {
                "price_change_pct_24h": 2.0,
                "price_change_pct_7d":  3.0,
                "volume_24h":           1_000_000_000.0,
                "avg_volume_30d":       1_000_000_000.0,
            }
        }
        result = agent.process(raw)
        for key in ("signal", "confidence", "symbols", "count"):
            assert key in result, f"Missing key '{key}' in OnChainAgent output"

    # ── edge: zero avg_volume ────────────────────────────────

    def test_zero_avg_volume_no_crash(self, agent):
        """avg_volume_30d=0 should not raise ZeroDivisionError."""
        raw = {
            "bitcoin": {
                "price_change_pct_24h": 3.0,
                "price_change_pct_7d":  4.0,
                "volume_24h":           1_000_000_000.0,
                "avg_volume_30d":       0.0,
            }
        }
        result = agent.process(raw)
        _assert_valid_signal(result, "OnChainAgent")


# ═══════════════════════════════════════════════════════════════════
#  VOLATILITY SURFACE AGENT
# ═══════════════════════════════════════════════════════════════════

class TestVolatilitySurfaceAgent:
    """
    Unit tests for VolatilitySurfaceAgent._compute_signal() and process().

    Signal logic (from source):
      • IV_spread = implied_vol - realized_vol_proxy
      • spread > 0.30  → -0.55 (fear / elevated IV)
      • spread < -0.10 → -0.15 (complacency)
      • -0.10 ≤ spread ≤ 0.10 → 0.0 (neutral)
      • hvol_7d_change_pct < -5 → boosts toward +0.15 (calming)
    """

    @pytest.fixture
    def agent(self, qt_app):
        from core.agents.volatility_surface_agent import VolatilitySurfaceAgent
        a = VolatilitySurfaceAgent()
        yield a
        a._stop_requested = True

    def _btc_data(self, implied_vol: float, realized_vol: float,
                  hvol_change: float = 0.0, index: float = 65_000.0) -> dict:
        return {
            "BTC": {
                "implied_vol": implied_vol,
                "realized_vol_proxy": realized_vol,
                "hvol_7d_change_pct": hvol_change,
                "index_price": index,
                "mark_price": index * 1.001,
            }
        }

    # ── empty input ──────────────────────────────────────────

    def test_empty_raw_returns_neutral(self, agent):
        result = agent.process({})
        _assert_valid_signal(result, "VolatilitySurfaceAgent")
        assert result["signal"] == 0.0
        assert result["count"] == 0

    # ── fear regime: high IV ─────────────────────────────────

    def test_high_iv_spread_bearish(self, agent):
        """
        implied_vol=0.80, realized=0.40 → spread=0.40 > 0.30 → signal = -0.55.
        """
        result = agent.process(self._btc_data(0.80, 0.40))
        _assert_valid_signal(result, "VolatilitySurfaceAgent")
        assert result["signal"] < -0.3, (
            f"Expected bearish fear-regime signal, got {result['signal']}"
        )

    # ── complacency: IV below realized ──────────────────────

    def test_iv_below_realized_complacency(self, agent):
        """
        implied_vol=0.30, realized=0.50 → spread=-0.20 < -0.10 → signal = -0.15.
        """
        result = agent.process(self._btc_data(0.30, 0.50))
        _assert_valid_signal(result, "VolatilitySurfaceAgent")
        assert result["signal"] < 0, (
            f"Expected mildly bearish complacency signal, got {result['signal']}"
        )

    # ── calming regime: declining historical vol ─────────────

    def test_calming_hvol_bullish(self, agent):
        """
        Neutral IV spread but historical vol declining 10% → calming regime.
        Expect: signal ≥ 0.
        """
        result = agent.process(self._btc_data(
            implied_vol=0.45, realized_vol=0.42, hvol_change=-10.0
        ))
        _assert_valid_signal(result, "VolatilitySurfaceAgent")
        assert result["signal"] >= 0.0, (
            f"Expected non-negative calming signal, got {result['signal']}"
        )

    # ── multiple currencies ──────────────────────────────────

    def test_btc_and_eth_both_processed(self, agent):
        """Both BTC and ETH data → count=2, both symbols in output."""
        raw = {
            "BTC": {
                "implied_vol": 0.75, "realized_vol_proxy": 0.40,
                "hvol_7d_change_pct": 0.0, "index_price": 65_000.0,
                "mark_price": 65_100.0,
            },
            "ETH": {
                "implied_vol": 0.80, "realized_vol_proxy": 0.35,
                "hvol_7d_change_pct": -3.0, "index_price": 3_500.0,
                "mark_price": 3_510.0,
            },
        }
        result = agent.process(raw)
        _assert_valid_signal(result, "VolatilitySurfaceAgent")
        assert result["count"] == 2
        assert "BTC" in result["symbols"]
        assert "ETH" in result["symbols"]

    # ── output includes iv_trend ─────────────────────────────

    def test_output_includes_iv_trend(self, agent):
        """process() must return an 'iv_trend' key with trend metadata."""
        result = agent.process(self._btc_data(0.60, 0.40))
        assert "iv_trend" in result, "Missing 'iv_trend' in VolatilitySurfaceAgent output"
        iv_trend = result["iv_trend"]
        assert "trend" in iv_trend

    # ── neutral zone ─────────────────────────────────────────

    def test_neutral_spread_zero_signal(self, agent):
        """IV spread = 0.05 (within ±0.10 neutral band) → signal near 0."""
        result = agent.process(self._btc_data(0.45, 0.40))  # spread=0.05
        _assert_valid_signal(result, "VolatilitySurfaceAgent")
        assert abs(result["signal"]) <= 0.20, (
            f"Expected near-zero neutral signal, got {result['signal']}"
        )


# ═══════════════════════════════════════════════════════════════════
#  WHALE TRACKING AGENT
# ═══════════════════════════════════════════════════════════════════

class TestWhaleTrackingAgent:
    """
    Unit tests for WhaleTrackingAgent._compute_signal() and process().

    Signal logic (from source):
      • ≥3 large txs with >60% inflow direction → -0.50 (exchange inflow / bearish)
      • outflow dominant + total_outflow > 100 BTC → +0.55 (accumulation / bullish)
      • inflow dominant + total_inflow > 100 BTC   → -0.65 (distribution / bearish)
      • balanced → 0.0
    """

    @pytest.fixture
    def agent(self, qt_app):
        from core.agents.whale_agent import WhaleTrackingAgent
        a = WhaleTrackingAgent()
        yield a
        a._stop_requested = True

    def _tx(self, amount_btc: float, direction: str) -> dict:
        return {
            "amount_btc": amount_btc,
            "direction": direction,
            "timestamp": _unix_now(),
            "address": "bc1qtest",
            "source": "test",
        }

    def _raw(self, txs: list) -> dict:
        return {"transactions": txs, "metadata": {"sources": ["test"]}}

    # ── empty ────────────────────────────────────────────────

    def test_empty_transactions_neutral(self, agent):
        """No transactions → neutral signal."""
        result = agent.process(self._raw([]))
        _assert_valid_signal(result, "WhaleTrackingAgent")
        assert result["signal"] == 0.0
        assert result["whale_count"] == 0

    # ── bearish: large inflow (inflow_selling branch) ────────
    # NOTE: len(txs) >= 3 triggers the spike branch (early return).
    # To reach inflow_selling (signal=-0.65), use 1-2 large txs so the
    # spike branch is skipped: inflow_ratio > 0.7 AND inflow_vol > 100 BTC.

    def test_large_inflow_distribution_bearish(self, agent):
        """
        1 large inflow tx (200 BTC) → inflow_ratio=1.0 > 0.7, vol > 100
        → inflow_selling branch → signal = -0.65.
        """
        txs = [self._tx(200.0, "inflow")]   # single tx: skips spike branch
        result = agent.process(self._raw(txs))
        _assert_valid_signal(result, "WhaleTrackingAgent")
        assert result["signal"] < 0, (
            f"Expected bearish inflow_selling signal, got {result['signal']}"
        )

    # ── bullish: large outflow (outflow_accumulation branch) ─
    # Use 1-2 large outflow txs so the spike branch (len>=3) is not triggered.

    def test_large_outflow_accumulation_bullish(self, agent):
        """
        1 large outflow tx (200 BTC) → inflow_ratio=0 < 0.3, outflow_vol > 100
        → outflow_accumulation branch → signal = +0.55.
        """
        txs = [self._tx(200.0, "outflow")]  # single tx: skips spike branch
        result = agent.process(self._raw(txs))
        _assert_valid_signal(result, "WhaleTrackingAgent")
        assert result["signal"] > 0, (
            f"Expected bullish outflow_accumulation signal, got {result['signal']}"
        )

    # ── neutral: balanced ────────────────────────────────────

    def test_balanced_inflow_outflow_neutral(self, agent):
        """Equal inflow and outflow volumes → near-neutral signal."""
        txs = (
            [self._tx(50.0, "inflow")  for _ in range(3)] +
            [self._tx(50.0, "outflow") for _ in range(3)]
        )
        result = agent.process(self._raw(txs))
        _assert_valid_signal(result, "WhaleTrackingAgent")
        # Balanced scenario — expect small absolute signal
        assert abs(result["signal"]) <= 0.55

    # ── output structure ────────────────────────────────────

    def test_output_structure_keys(self, agent):
        """Verify all documented output keys are present."""
        txs = [self._tx(50.0, "outflow")]
        result = agent.process(self._raw(txs))
        for key in ("signal", "confidence", "whale_count",
                    "total_volume_btc", "dominant_direction", "transactions"):
            assert key in result, f"Missing key '{key}' in WhaleTrackingAgent output"

    # ── single small tx ──────────────────────────────────────

    def test_single_small_tx_neutral(self, agent):
        """Single small transaction (1 BTC) → minimal signal."""
        result = agent.process(self._raw([self._tx(1.0, "inflow")]))
        _assert_valid_signal(result, "WhaleTrackingAgent")

    # ── spike direction labels (len >= 3 early-return branch) ──

    def test_dominant_direction_spike_down_for_inflow(self, agent):
        """
        ≥3 txs with inflow_ratio > 0.6 → spike branch fires immediately
        → dominant_direction = 'spike_down', signal = +0.5.
        """
        txs = [self._tx(100.0, "inflow") for _ in range(4)]
        result = agent.process(self._raw(txs))
        assert result["dominant_direction"] == "spike_down", (
            f"Expected 'spike_down' for inflow spike, got {result['dominant_direction']}"
        )
        assert result["signal"] == pytest.approx(0.5)

    def test_dominant_direction_spike_up_for_outflow(self, agent):
        """
        ≥3 txs with inflow_ratio < 0.4 (outflow-dominant) → spike branch
        → dominant_direction = 'spike_up', signal = -0.5.
        """
        txs = [self._tx(100.0, "outflow") for _ in range(4)]
        result = agent.process(self._raw(txs))
        assert result["dominant_direction"] == "spike_up", (
            f"Expected 'spike_up' for outflow spike, got {result['dominant_direction']}"
        )
        assert result["signal"] == pytest.approx(-0.5)


# ═══════════════════════════════════════════════════════════════════
#  LIQUIDATION FLOW AGENT
# ═══════════════════════════════════════════════════════════════════

class TestLiquidationFlowAgent:
    """
    Unit tests for LiquidationFlowAgent._compute_signal() and process().

    Signal logic (from source, _LIQ_RATIO_THRESHOLD = 2.0):
      • long_liq > short_liq × 2.0  → signal = -0.75 (bearish, longs liquidated)
      • short_liq > long_liq × 2.0  → signal = +0.75 (bullish, shorts liquidated)
      • balanced (ratio < 2.0)       → signal =  0.0
    """

    @pytest.fixture
    def agent(self, qt_app):
        from core.agents.liquidation_flow_agent import LiquidationFlowAgent
        a = LiquidationFlowAgent()
        yield a
        a._stop_requested = True

    def _sym(self, long_vol: float, short_vol: float, symbol: str = "BTC/USDT") -> dict:
        return {
            symbol: {
                "symbol": symbol,
                "long_liq_volume":  long_vol,
                "short_liq_volume": short_vol,
                "has_liquidations": (long_vol + short_vol) > 0,
            }
        }

    # ── empty ────────────────────────────────────────────────

    def test_empty_raw_neutral(self, agent):
        """Empty raw dict → neutral, count=0."""
        result = agent.process({})
        _assert_valid_signal(result, "LiquidationFlowAgent")
        assert result["signal"] == 0.0
        assert result["count"] == 0

    # ── bearish: long liquidations dominant ──────────────────

    def test_long_liq_dominant_bearish(self, agent):
        """
        long_liq=10M, short_liq=1M → ratio=10 >> 2.0 threshold → signal = -0.75.
        """
        result = agent.process(self._sym(10_000_000, 1_000_000))
        _assert_valid_signal(result, "LiquidationFlowAgent")
        assert result["signal"] == pytest.approx(-0.75), (
            f"Expected -0.75 for long-dominant liquidations, got {result['signal']}"
        )
        assert result["confidence"] >= 0.50

    # ── bullish: short liquidations dominant ─────────────────

    def test_short_liq_dominant_bullish(self, agent):
        """
        short_liq=10M, long_liq=1M → ratio=10 >> 2.0 threshold → signal = +0.75.
        """
        result = agent.process(self._sym(1_000_000, 10_000_000))
        _assert_valid_signal(result, "LiquidationFlowAgent")
        assert result["signal"] == pytest.approx(0.75), (
            f"Expected +0.75 for short-dominant liquidations, got {result['signal']}"
        )

    # ── neutral: balanced liquidations ──────────────────────

    def test_balanced_liquidations_neutral(self, agent):
        """
        Equal long and short liquidations → ratio=1 < 2.0 → signal = 0.0.
        """
        result = agent.process(self._sym(5_000_000, 5_000_000))
        _assert_valid_signal(result, "LiquidationFlowAgent")
        assert result["signal"] == pytest.approx(0.0)

    # ── below threshold ──────────────────────────────────────

    def test_ratio_below_threshold_neutral(self, agent):
        """
        long_liq=1.5× short_liq — still below 2.0 threshold → 0.0.
        """
        result = agent.process(self._sym(1_500_000, 1_000_000))
        _assert_valid_signal(result, "LiquidationFlowAgent")
        assert result["signal"] == pytest.approx(0.0)

    # ── multiple symbols aggregated ─────────────────────────

    def test_multiple_symbols_averaged(self, agent):
        """
        BTC: bearish (-0.75) + ETH: bullish (+0.75) → averaged near 0.
        """
        raw = {}
        raw.update(self._sym(10_000_000, 1_000_000, "BTC/USDT"))
        raw.update(self._sym(1_000_000, 10_000_000, "ETH/USDT"))
        result = agent.process(raw)
        _assert_valid_signal(result, "LiquidationFlowAgent")
        assert result["count"] == 2
        # Average of -0.75 and +0.75 = 0.0
        assert abs(result["signal"]) < 0.1

    # ── output structure ────────────────────────────────────

    def test_output_structure_keys(self, agent):
        """Verify documented output keys."""
        result = agent.process(self._sym(1_000_000, 500_000))
        for key in ("signal", "confidence", "symbols", "count"):
            assert key in result, f"Missing key '{key}' in LiquidationFlowAgent output"


# ═══════════════════════════════════════════════════════════════════
#  NARRATIVE SHIFT AGENT
# ═══════════════════════════════════════════════════════════════════

class TestNarrativeShiftAgent:
    """
    Unit tests for NarrativeShiftAgent.process().

    Signal logic (from _compute_signal()):
      • dominant in {hack_exploit, regulatory}            → -0.75, conf=0.80
      • shift_score > 0.4 + bullish narrative dominant    → +0.60, conf=0.75
      • shift_score > 0.4 + bearish narrative dominant    → -0.70, conf=0.75
      • same narrative, sentiment > 0.3                   → +0.20, conf=0.40
      • same narrative, sentiment < -0.3                  → -0.20, conf=0.40

    The ML scorer is mocked to avoid model loading in tests.
    """

    @pytest.fixture
    def agent(self, qt_app):
        from core.agents.narrative_agent import NarrativeShiftAgent
        a = NarrativeShiftAgent()
        # _compute_narrative_sentiment calls scorer(text) directly (not scorer.score()).
        # MagicMock(return_value=0.0) makes scorer("any text") → 0.0 (neutral float).
        scorer_mock = MagicMock(return_value=0.0)
        a._scorer = scorer_mock
        yield a
        a._stop_requested = True

    def _articles(self, texts: list[str]) -> dict:
        return {
            "articles": [
                {"title": t, "body": "", "source": "test", "url": ""}
                for t in texts
            ],
            "count": len(texts),
        }

    # ── empty ────────────────────────────────────────────────

    def test_empty_articles_neutral(self, agent):
        """No articles → neutral 0.0 signal."""
        result = agent.process({"articles": [], "count": 0})
        _assert_valid_signal(result, "NarrativeShiftAgent")
        assert result["signal"] == 0.0
        assert result["dominant_narrative"] is None

    # ── hack/exploit bearish override ───────────────────────

    def test_hack_exploit_dominant_strong_bearish(self, agent):
        """
        Multiple hack/exploit headlines → dominant='hack_exploit' → signal=-0.75.
        """
        texts = [
            "Major DeFi bridge hack drains $300M in exploit",
            "Protocol exploit: attacker steals funds via reentrancy",
            "Another hack reported — bridge attack on LayerZero",
            "Rug pull confirmed: exit scam by anon dev team",
        ]
        result = agent.process(self._articles(texts))
        _assert_valid_signal(result, "NarrativeShiftAgent")
        assert result["signal"] == pytest.approx(-0.75), (
            f"Hack/exploit dominant: expected -0.75, got {result['signal']}"
        )
        assert result["confidence"] == pytest.approx(0.80)

    # ── regulatory bearish override ──────────────────────────

    def test_regulatory_dominant_strong_bearish(self, agent):
        """
        Regulatory headlines → dominant='regulatory' → signal=-0.75.
        """
        texts = [
            "SEC files enforcement action against crypto exchange",
            "CFTC regulation of DeFi protocols raises concerns",
            "New crypto ban under consideration by regulators",
            "Legal framework for crypto assets under SEC review",
        ]
        result = agent.process(self._articles(texts))
        _assert_valid_signal(result, "NarrativeShiftAgent")
        assert result["signal"] == pytest.approx(-0.75), (
            f"Regulatory dominant: expected -0.75, got {result['signal']}"
        )

    # ── bullish narrative shift ──────────────────────────────

    def test_btc_etf_detected_as_dominant(self, agent):
        """
        BTC ETF articles with no previous narrative: dominant='btc_etf' is set.
        Shift score = 0.0 (previous is None → no shift computed), mocked scorer
        returns 0.0 sentiment → signal = 0.0 (neutral, no prior to shift from).
        The key assertion is that the narrative is correctly identified.
        """
        agent._previous_dominant = None
        texts = [
            "BlackRock BTC ETF sees record inflows as approval nears",
            "Bitcoin ETF approved: institutional investors pile in",
            "ETF flows for Bitcoin hit $500M in a single day",
        ]
        result = agent.process(self._articles(texts))
        _assert_valid_signal(result, "NarrativeShiftAgent")
        assert result["dominant_narrative"] == "btc_etf"
        # No previous narrative → shift_score = 0.0 → no shift signal triggered
        assert result["narrative_shift_score"] == pytest.approx(0.0)

    def test_btc_etf_shift_from_different_prev_narrative(self, agent):
        """
        When previous narrative was 'regulatory' and new dominant is 'btc_etf',
        a shift is detected → signal = +0.60.
        """
        agent._previous_dominant = "regulatory"
        texts = [
            "BlackRock BTC ETF sees record inflows as approval nears",
            "Bitcoin ETF approved: institutional investors pile in",
            "ETF flows for Bitcoin hit $500M in a single day",
        ]
        result = agent.process(self._articles(texts))
        _assert_valid_signal(result, "NarrativeShiftAgent")
        assert result["signal"] == pytest.approx(0.60), (
            f"btc_etf shift from regulatory: expected +0.60, got {result['signal']}"
        )

    # ── output structure ────────────────────────────────────

    def test_output_structure_keys(self, agent):
        """All documented output keys must be present."""
        result = agent.process(self._articles(["some generic article"]))
        for key in (
            "signal", "confidence", "dominant_narrative", "previous_narrative",
            "narrative_shift_score", "narrative_sentiment",
            "top_articles", "article_count",
        ):
            assert key in result, f"Missing '{key}' in NarrativeShiftAgent output"

    # ── macro_risk bearish shift ─────────────────────────────

    def test_macro_risk_shift_bearish(self, agent):
        """
        Shift from btc_etf to macro_risk (bearish narrative) → signal = -0.70.
        """
        agent._previous_dominant = "btc_etf"
        texts = [
            "Federal Reserve signals rate hikes amid global recession fears",
            "Macro risk: inflation and recession fears rattle crypto markets",
            "Rising yields drive macro-risk concerns across crypto sector",
        ]
        result = agent.process(self._articles(texts))
        _assert_valid_signal(result, "NarrativeShiftAgent")
        assert result["signal"] <= -0.50, (
            f"macro_risk shift from btc_etf: expected ≤ -0.50, got {result['signal']}"
        )


# ═══════════════════════════════════════════════════════════════════
#  SOCIAL SENTIMENT AGENT
# ═══════════════════════════════════════════════════════════════════

class TestSocialSentimentAgent:
    """
    Unit tests for SocialSentimentAgent.process().

    Component scores (no ML model — all heuristic):
      • FNG value ≤ 15                → -0.70, conf=0.80 (extreme fear)
      • FNG value ≥ 85                → +0.70, conf=0.80 (extreme greed)
      • BTC dominance ≥ 60%           → -0.30, conf=0.50 (risk-off)
      • BTC dominance ≤ 40%           → +0.30, conf=0.50 (alt season)
      • LunarCrush galaxy_score ≥ 60  → +0.60, conf=0.70
      • LunarCrush galaxy_score ≤ 40  → -0.60, conf=0.70
    """

    @pytest.fixture
    def agent(self, qt_app):
        from core.agents.social_sentiment_agent import SocialSentimentAgent
        a = SocialSentimentAgent()
        yield a
        a._stop_requested = True

    # ── empty ────────────────────────────────────────────────

    def test_empty_raw_neutral(self, agent):
        """Empty raw dict returns neutral output without crashing."""
        result = agent.process({})
        _assert_valid_signal(result, "SocialSentimentAgent")
        assert result["signal"] == pytest.approx(0.0)
        assert "sentiment_label" in result

    # ── extreme fear FNG ─────────────────────────────────────

    def test_extreme_fear_fng_bearish(self, agent):
        """
        Fear & Greed Index = 8 (extreme fear) → strong bearish signal.
        """
        raw = {"fng": {"value": 8, "value_classification": "Extreme Fear"}}
        result = agent.process(raw)
        _assert_valid_signal(result, "SocialSentimentAgent")
        assert result["signal"] < -0.40, (
            f"Extreme fear FNG: expected signal < -0.40, got {result['signal']}"
        )

    # ── extreme greed FNG ────────────────────────────────────

    def test_extreme_greed_fng_bullish(self, agent):
        """
        Fear & Greed Index = 92 (extreme greed) → strong bullish signal.
        """
        raw = {"fng": {"value": 92, "value_classification": "Extreme Greed"}}
        result = agent.process(raw)
        _assert_valid_signal(result, "SocialSentimentAgent")
        assert result["signal"] > 0.40, (
            f"Extreme greed FNG: expected signal > 0.40, got {result['signal']}"
        )

    # ── high BTC dominance (risk-off) ───────────────────────

    def test_high_btc_dominance_risk_off(self, agent):
        """
        BTC dominance = 68% → alts risk-off → bearish component.
        """
        raw = {
            "global": {
                "btc_dominance": 68.0,
                "eth_dominance": 15.0,
                "total_market_cap_change_pct": -2.0,
            }
        }
        result = agent.process(raw)
        _assert_valid_signal(result, "SocialSentimentAgent")
        assert result["signal"] < 0, (
            f"High BTC dominance: expected bearish signal, got {result['signal']}"
        )

    # ── low BTC dominance (alt season) ──────────────────────

    def test_low_btc_dominance_alt_season(self, agent):
        """
        BTC dominance = 35% → alt season → bullish component.
        """
        raw = {
            "global": {
                "btc_dominance": 35.0,
                "eth_dominance": 22.0,
                "total_market_cap_change_pct": 3.5,
            }
        }
        result = agent.process(raw)
        _assert_valid_signal(result, "SocialSentimentAgent")
        assert result["signal"] > 0, (
            f"Low BTC dominance: expected bullish signal, got {result['signal']}"
        )

    # ── LunarCrush high galaxy ───────────────────────────────

    def test_lunarcrush_high_galaxy_bullish(self, agent):
        """
        LunarCrush galaxy_score = 75 → strong bullish social momentum.
        """
        raw = {
            "lunarcrush": {
                "avg_galaxy_score": 75.0,
                "avg_alt_rank": 200.0,
                "top_coins": ["BTC", "ETH"],
            }
        }
        result = agent.process(raw)
        _assert_valid_signal(result, "SocialSentimentAgent")
        assert result["signal"] > 0.30, (
            f"High LunarCrush galaxy: expected signal > 0.30, got {result['signal']}"
        )

    # ── combined components ──────────────────────────────────

    def test_combined_fear_greed_and_dominance(self, agent):
        """
        FNG extreme fear AND high BTC dominance → both bearish → signal < -0.2.
        """
        raw = {
            "fng": {"value": 12, "value_classification": "Extreme Fear"},
            "global": {
                "btc_dominance": 65.0,
                "eth_dominance": 17.0,
                "total_market_cap_change_pct": -4.0,
            },
        }
        result = agent.process(raw)
        _assert_valid_signal(result, "SocialSentimentAgent")
        assert result["signal"] < -0.2
        assert "components" in result

    # ── output structure ────────────────────────────────────

    def test_output_structure_keys(self, agent):
        """All documented output keys must be present."""
        raw = {"fng": {"value": 50, "value_classification": "Neutral"}}
        result = agent.process(raw)
        for key in ("signal", "confidence", "sentiment_label", "components"):
            assert key in result, f"Missing '{key}' in SocialSentimentAgent output"


# ═══════════════════════════════════════════════════════════════════
#  REDDIT SENTIMENT AGENT
# ═══════════════════════════════════════════════════════════════════

class TestRedditSentimentAgent:
    """
    Unit tests for RedditSentimentAgent.process().

    _score_text() is patched to return deterministic values so tests
    don't depend on VADER installation or ML model loading.

    Score adjustments (from source):
      • post_score / 1000 capped at 2× amplification of signal
      • upvote_ratio > 0.95 and positive signal → +0.20 confidence
      • 'daily'/'discussion' flair → conf × 0.8
      • 'dd' flair + sig > 0.5 + score > 500 → sig + 0.2, conf × 1.2
      • crash keywords in top post (score > 100) → sig - 0.50, conf × 1.2
    """

    @pytest.fixture
    def agent(self, qt_app):
        from core.agents.reddit_agent import RedditSentimentAgent
        a = RedditSentimentAgent()
        yield a
        a._stop_requested = True

    def _post(self, title: str, score: int = 100,
              upvote_ratio: float = 0.85, flair: str = "") -> dict:
        return {
            "title": title,
            "selftext": "",
            "score": score,
            "num_comments": 50,
            "upvote_ratio": upvote_ratio,
            "link_flair_text": flair,
            "url": "https://reddit.com/r/test",
            "created_utc": _unix_now(),
        }

    def _raw(self, posts_by_sub: dict) -> dict:
        return {
            sub: {"posts": posts}
            for sub, posts in posts_by_sub.items()
        }

    # ── empty ────────────────────────────────────────────────

    def test_empty_raw_neutral(self, agent):
        """Empty input → neutral output without crashing."""
        result = agent.process({})
        _assert_valid_signal(result, "RedditSentimentAgent")
        assert result["signal"] == 0.0
        assert result["post_count"] == 0

    def test_empty_posts_list_neutral(self, agent):
        """Subreddit present but with empty posts list → neutral."""
        result = agent.process(self._raw({"bitcoin": []}))
        _assert_valid_signal(result, "RedditSentimentAgent")
        assert result["signal"] == 0.0

    # ── bullish posts ────────────────────────────────────────

    def test_bullish_posts_positive_signal(self, agent):
        """
        Uniformly bullish-scored posts → aggregated signal > 0.
        Patches _score_text to return (0.70, 0.75) for each post.
        """
        with patch.object(agent, "_score_text", return_value=(0.70, 0.75)):
            posts = [self._post("Bitcoin is going to the moon!") for _ in range(5)]
            result = agent.process(self._raw({"bitcoin": posts}))
        _assert_valid_signal(result, "RedditSentimentAgent")
        assert result["signal"] > 0, (
            f"Expected positive signal from bullish posts, got {result['signal']}"
        )
        assert result["post_count"] == 5

    # ── bearish posts with crash keywords ───────────────────

    def test_crash_keyword_in_top_post_penalized(self, agent):
        """
        High-score post containing crash keywords gets a -0.50 penalty applied.
        Starting from a moderately bullish score (0.30), the penalty pulls it
        well below the raw score.
        """
        with patch.object(agent, "_score_text", return_value=(0.30, 0.60)):
            # High score (>100) + crash keyword in title → penalty fires
            post = self._post(
                "CRASH incoming — crypto dumped hard, very bearish outlook",
                score=500,
            )
            result = agent.process(self._raw({"bitcoin": [post]}))

        _assert_valid_signal(result, "RedditSentimentAgent")
        # The -0.5 penalty should pull signal below the raw scorer value
        assert result["signal"] < 0.30, (
            f"Crash-keyword penalty not applied correctly; signal={result['signal']}"
        )

    # ── DD flair boost ───────────────────────────────────────

    def test_dd_flair_boosts_high_score_bullish_post(self, agent):
        """
        DD flair + signal > 0.5 + post score > 500 → signal += 0.20.
        Base scorer returns 0.65 → post-boost should be capped at 1.0 but > 0.65.
        """
        with patch.object(agent, "_score_text", return_value=(0.65, 0.70)):
            post = self._post("DD: BTC breakout incoming, full analysis",
                              score=800, flair="DD")
            result = agent.process(self._raw({"CryptoCurrency": [post]}))

        _assert_valid_signal(result, "RedditSentimentAgent")
        assert result["signal"] > 0.5, (
            f"DD flair boost: expected signal > 0.5, got {result['signal']}"
        )

    # ── subreddits_checked populated ────────────────────────

    def test_subreddits_checked_populated(self, agent):
        """subreddits_checked must list all subreddits in raw."""
        with patch.object(agent, "_score_text", return_value=(0.0, 0.3)):
            raw = self._raw({
                "bitcoin": [self._post("test")],
                "CryptoCurrency": [self._post("test2")],
            })
            result = agent.process(raw)
        assert set(result["subreddits_checked"]) == {"bitcoin", "CryptoCurrency"}

    # ── output structure ────────────────────────────────────

    def test_output_structure_keys(self, agent):
        """All documented output keys must be present."""
        with patch.object(agent, "_score_text", return_value=(0.0, 0.3)):
            result = agent.process(self._raw(
                {"bitcoin": [self._post("Bitcoin is holding support")]}
            ))
        for key in (
            "signal", "confidence", "sentiment_label",
            "post_count", "avg_upvotes", "avg_comments",
            "top_posts", "subreddits_checked",
        ):
            assert key in result, f"Missing '{key}' in RedditSentimentAgent output"

    # ── multiple subreddits ──────────────────────────────────

    def test_multiple_subreddits_aggregated(self, agent):
        """
        Posts from 3 subreddits are all aggregated into one signal.
        """
        with patch.object(agent, "_score_text", return_value=(0.50, 0.60)):
            raw = self._raw({
                "bitcoin":          [self._post("Bullish BTC") for _ in range(3)],
                "CryptoCurrency":   [self._post("Crypto pumping") for _ in range(2)],
                "CryptoMarkets":    [self._post("Markets looking green")],
            })
            result = agent.process(raw)
        _assert_valid_signal(result, "RedditSentimentAgent")
        assert result["post_count"] == 6
        assert len(result["subreddits_checked"]) == 3


# ═══════════════════════════════════════════════════════════════════
#  CROSS-AGENT VALIDATION
# ═══════════════════════════════════════════════════════════════════

class TestAllRemainingAgentsSignalContract:
    """
    Parametrized smoke tests ensuring every remaining agent satisfies the
    universal NexusTrader signal contract (signal ∈ [-1,+1], conf ∈ [0,1],
    no NaN/Inf) even when fed minimal / edge-case inputs.
    """

    @pytest.mark.parametrize("agent_cls,raw", [
        (
            "core.agents.onchain_agent.OnChainAgent",
            {
                "bitcoin": {
                    "price_change_pct_24h": 0.0,
                    "price_change_pct_7d": 0.0,
                    "volume_24h": 1e9,
                    "avg_volume_30d": 1e9,
                }
            },
        ),
        (
            "core.agents.volatility_surface_agent.VolatilitySurfaceAgent",
            {
                "BTC": {
                    "implied_vol": 0.50,
                    "realized_vol_proxy": 0.45,
                    "hvol_7d_change_pct": 0.0,
                    "index_price": 65_000.0,
                    "mark_price": 65_100.0,
                }
            },
        ),
        (
            "core.agents.whale_agent.WhaleTrackingAgent",
            {
                "transactions": [
                    {
                        "amount_btc": 50.0,
                        "direction": "outflow",
                        "timestamp": 1_700_000_000,
                        "address": "bc1qtest",
                        "source": "test",
                    }
                ],
                "metadata": {"sources": ["test"]},
            },
        ),
        (
            "core.agents.liquidation_flow_agent.LiquidationFlowAgent",
            {
                "BTC/USDT": {
                    "symbol": "BTC/USDT",
                    "long_liq_volume": 1_000_000.0,
                    "short_liq_volume": 1_000_000.0,
                    "has_liquidations": True,
                }
            },
        ),
        (
            "core.agents.social_sentiment_agent.SocialSentimentAgent",
            {"fng": {"value": 50, "value_classification": "Neutral"}},
        ),
    ])
    def test_signal_contract(self, qt_app, agent_cls: str, raw: dict):
        module_path, class_name = agent_cls.rsplit(".", 1)
        import importlib
        module = importlib.import_module(module_path)
        cls = getattr(module, class_name)
        agent = cls()
        try:
            result = agent.process(raw)
            _assert_valid_signal(result, class_name)
        finally:
            agent._stop_requested = True

    def test_narrative_agent_contract(self, qt_app):
        """NarrativeShiftAgent with mocked scorer satisfies signal contract."""
        from core.agents.narrative_agent import NarrativeShiftAgent
        a = NarrativeShiftAgent()
        scorer_mock = MagicMock()
        scorer_mock.score.return_value = [(0.0, 0.3)]
        a._scorer = scorer_mock
        try:
            result = a.process({
                "articles": [
                    {"title": "Bitcoin price rallies", "body": "", "source": "test", "url": ""}
                ],
                "count": 1,
            })
            _assert_valid_signal(result, "NarrativeShiftAgent")
        finally:
            a._stop_requested = True

    def test_reddit_agent_contract(self, qt_app):
        """RedditSentimentAgent with patched scorer satisfies signal contract."""
        from core.agents.reddit_agent import RedditSentimentAgent
        a = RedditSentimentAgent()
        try:
            with patch.object(a, "_score_text", return_value=(0.10, 0.50)):
                result = a.process({
                    "bitcoin": {
                        "posts": [
                            {
                                "title": "BTC holding $60k",
                                "selftext": "",
                                "score": 200,
                                "num_comments": 50,
                                "upvote_ratio": 0.88,
                                "link_flair_text": "",
                                "url": "https://reddit.com/r/bitcoin/test",
                                "created_utc": _unix_now(),
                            }
                        ]
                    }
                })
            _assert_valid_signal(result, "RedditSentimentAgent")
        finally:
            a._stop_requested = True


# ═══════════════════════════════════════════════════════════════════
#  MIXED-TYPE DICT REGRESSION TESTS
#  Verifies that the stablecoin _check_depegs() bug pattern does NOT
#  exist in any of these agents (regression guard).
# ═══════════════════════════════════════════════════════════════════

class TestMixedTypeDictRegression:
    """
    Regression tests verifying that agents which iterate over dicts do not
    crash when the dict contains non-dict values (like the stablecoin
    _check_depegs() bug where _bitcoin_market_cap was stored as a float).
    """

    def test_onchain_agent_handles_non_dict_symbol_values(self, qt_app):
        """
        OnChainAgent.process() iterates raw.items() — if a value is not a dict
        (e.g., a float or None), it must not crash.
        """
        from core.agents.onchain_agent import OnChainAgent
        a = OnChainAgent()
        try:
            # Mix a valid symbol entry with a non-dict sentinel value
            raw = {
                "bitcoin": {
                    "price_change_pct_24h": 1.0,
                    "price_change_pct_7d": 2.0,
                    "volume_24h": 1e9,
                    "avg_volume_30d": 1e9,
                },
                "_meta_float": 99.99,   # sentinel non-dict value
            }
            # Should not raise AttributeError
            try:
                result = a.process(raw)
                # If it processes without error, verify output is valid
                _assert_valid_signal(result, "OnChainAgent")
            except (AttributeError, TypeError) as e:
                pytest.fail(
                    f"OnChainAgent.process() crashed on mixed-type dict: {e}. "
                    "Apply isinstance(data, dict) guard (same fix as stablecoin bug)."
                )
        finally:
            a._stop_requested = True

    def test_liquidation_agent_handles_non_dict_symbol_values(self, qt_app):
        """
        LiquidationFlowAgent.process() iterates raw.items() — non-dict values
        must not crash.
        """
        from core.agents.liquidation_flow_agent import LiquidationFlowAgent
        a = LiquidationFlowAgent()
        try:
            raw = {
                "BTC/USDT": {
                    "symbol": "BTC/USDT",
                    "long_liq_volume": 1_000_000.0,
                    "short_liq_volume": 500_000.0,
                    "has_liquidations": True,
                },
                "_timestamp": 1_700_000_000,   # sentinel non-dict value
            }
            try:
                result = a.process(raw)
                _assert_valid_signal(result, "LiquidationFlowAgent")
            except (AttributeError, TypeError) as e:
                pytest.fail(
                    f"LiquidationFlowAgent.process() crashed on mixed-type dict: {e}. "
                    "Apply isinstance(data, dict) guard."
                )
        finally:
            a._stop_requested = True
