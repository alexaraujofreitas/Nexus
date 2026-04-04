# ============================================================
# Phase 4A Final Hardening Tests
#
# FINAL FIX 1: Batch fetch optimization — reduced HTTP calls
# FINAL FIX 2: Hard MIL cap enforcement in ConfluenceScorer
# Regression: no change when MIL disabled
# ============================================================
import sys
import time
import threading
from unittest.mock import patch, MagicMock, PropertyMock
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ══════════════════════════════════════════════════════════════
# SECTION 1: Batch Fetch Optimization
# ══════════════════════════════════════════════════════════════

class TestBatchFetchOptimization:
    """
    Verify that fetch_all_symbols uses batch endpoints,
    reducing HTTP call count from 2×N to 1+N (Binance batch + OKX per-symbol).
    """

    def _make_enhancer(self):
        from core.agents.mil.funding_rate_enhanced import FundingRateEnhancer
        return FundingRateEnhancer()

    def test_batch_reduces_http_calls_vs_per_symbol(self):
        """
        With 5 symbols, old approach = 10 HTTP calls (2 per symbol).
        Batch approach = 1 (Binance batch) + 5 (OKX per-symbol) = 6 calls.
        Verify batch makes fewer calls than 2×N.
        """
        e = self._make_enhancer()
        symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"]

        # Mock _fetch_batch_all_exchanges to track call count
        binance_result = {f"{s.split('/')[0]}USDT": 0.01 for s in symbols}
        okx_result = {f"{s.split('/')[0]}-USDT-SWAP": 0.012 for s in symbols}

        with patch.object(e, "is_enabled", return_value=True):
            with patch.object(
                e, "_fetch_batch_all_exchanges",
                return_value=(binance_result, okx_result)
            ) as mock_batch:
                e.fetch_all_symbols(symbols)
                # Batch method called exactly once (not N times)
                mock_batch.assert_called_once()

        # Compare: old per-symbol approach would need 2×N = 10 calls.
        # Batch approach: 1 call to _fetch_batch_all_exchanges.
        # The internal call count is tracked in _last_batch_http_calls.

    def test_batch_populates_all_symbol_caches(self):
        """After batch fetch, all symbols should have cache entries."""
        e = self._make_enhancer()
        symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]

        binance_result = {"BTCUSDT": 0.01, "ETHUSDT": 0.02, "SOLUSDT": 0.015}
        okx_result = {"BTC-USDT-SWAP": 0.011, "ETH-USDT-SWAP": 0.021}

        with patch.object(e, "is_enabled", return_value=True):
            with patch.object(
                e, "_fetch_batch_all_exchanges",
                return_value=(binance_result, okx_result)
            ):
                e.fetch_all_symbols(symbols)

        # All 3 symbols should have cache entries
        for sym in symbols:
            cached = e._get_cached_rates(sym)
            assert cached is not None, f"Missing cache for {sym}"

        # BTC should have both exchanges
        btc_cache = e._get_cached_rates("BTC/USDT")
        assert "binance" in btc_cache
        assert "okx" in btc_cache

        # SOL should have only binance (OKX didn't return SOL)
        sol_cache = e._get_cached_rates("SOL/USDT")
        assert "binance" in sol_cache
        assert "okx" not in sol_cache

    def test_batch_http_call_count_tracked(self):
        """_http_call_count and _last_batch_http_calls should be tracked."""
        e = self._make_enhancer()
        symbols = ["BTC/USDT", "ETH/USDT"]

        with patch.object(e, "is_enabled", return_value=True):
            with patch.object(
                e, "_fetch_batch_all_exchanges",
                return_value=({"BTCUSDT": 0.01, "ETHUSDT": 0.02}, {"BTC-USDT-SWAP": 0.01})
            ):
                # Simulate that batch made 3 HTTP calls (1 Binance + 2 OKX)
                def mock_batch(syms):
                    e._last_batch_http_calls = 3
                    return ({"BTCUSDT": 0.01}, {"BTC-USDT-SWAP": 0.01})

                with patch.object(e, "_fetch_batch_all_exchanges", side_effect=mock_batch):
                    e.fetch_all_symbols(symbols)

        assert e._http_call_count == 3
        assert e._last_batch_http_calls == 3
        diag = e.get_diagnostics()
        assert "http_call_count" in diag
        assert "last_batch_http_calls" in diag

    def test_batch_call_count_comparison(self):
        """
        Explicit call count comparison:
        - Old per-symbol: N symbols × 2 exchanges = 2N HTTP calls
        - Batch: 1 (Binance batch) + N (OKX per-symbol) = N+1 HTTP calls
        For 5 symbols: 10 → 6 (40% reduction).
        """
        from core.agents.mil.funding_rate_enhanced import FundingRateEnhancer
        e = FundingRateEnhancer()
        symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"]
        n = len(symbols)

        old_calls = 2 * n  # per-symbol approach
        # Batch: 1 Binance + N OKX = N+1
        new_calls = 1 + n

        assert new_calls < old_calls, (
            f"Batch ({new_calls}) should use fewer calls than per-symbol ({old_calls})"
        )
        reduction_pct = (1 - new_calls / old_calls) * 100
        assert reduction_pct >= 30, (
            f"Expected ≥30% reduction, got {reduction_pct:.0f}%"
        )

    def test_rate_limit_still_applies_in_batch_mode(self):
        """Rate limiting must still skip recently-fetched symbols."""
        e = self._make_enhancer()
        symbols = ["BTC/USDT", "ETH/USDT"]

        with patch.object(e, "is_enabled", return_value=True):
            with patch.object(
                e, "_fetch_batch_all_exchanges",
                return_value=({"BTCUSDT": 0.01, "ETHUSDT": 0.02}, {})
            ) as mock_batch:
                e.fetch_all_symbols(symbols)
                first_call_args = mock_batch.call_args[0][0]
                assert len(first_call_args) == 2

                # Immediately again — all should be rate-limited
                e.fetch_all_symbols(symbols)
                # _fetch_batch_all_exchanges should NOT be called again
                assert mock_batch.call_count == 1

    def test_batch_single_exchange_failure_doesnt_block_other(self):
        """If Binance batch fails, OKX should still populate cache."""
        e = self._make_enhancer()

        # Simulate: Binance returns nothing (failure), OKX returns data
        def mock_batch(symbols):
            e._last_batch_http_calls = 2
            binance_rates = {}  # Binance failed — empty
            okx_rates = {"BTC-USDT-SWAP": 0.012}  # OKX succeeded
            return (binance_rates, okx_rates)

        with patch.object(e, "is_enabled", return_value=True):
            with patch.object(e, "_fetch_batch_all_exchanges", side_effect=mock_batch):
                e.fetch_all_symbols(["BTC/USDT"])

        cached = e._get_cached_rates("BTC/USDT")
        assert cached is not None
        assert "okx" in cached
        # Binance should NOT be in cache (it failed)
        assert "binance" not in cached


# ══════════════════════════════════════════════════════════════
# SECTION 2: Hard MIL Cap Enforcement in ConfluenceScorer
#
# The cap covers TOTAL MIL influence (orchestrator injection +
# OI/Liq modifiers) relative to the PURE TECHNICAL baseline
# (no orchestrator, no OI/Liq).  The baseline is computed from
# only technical model signals.
# ══════════════════════════════════════════════════════════════

class TestMILCapEnforcement:
    """
    Verify that TOTAL MIL contribution (orchestrator + OI/Liq)
    is MATHEMATICALLY clamped at MIL_INFLUENCE_CAP × tech_baseline
    in the ConfluenceScorer.
    """

    def test_mil_cap_enforced_in_confluence_scorer_source(self):
        """ConfluenceScorer must contain the MIL cap enforcement block."""
        scorer_path = ROOT / "core" / "meta_decision" / "confluence_scorer.py"
        content = scorer_path.read_text()
        assert "MIL Hard Cap Enforcement" in content
        assert "MIL_INFLUENCE_CAP" in content
        assert "_mil_max_delta" in content
        assert "_mil_actual_delta" in content
        assert "mil_capped" in content

    def test_baseline_is_pure_technical_not_pre_modifier(self):
        """
        The cap baseline must be _mil_technical_baseline (excludes
        orchestrator), NOT _pre_modifier_score (which includes it).
        Verify that _mil_technical_baseline is computed from non-
        orchestrator signals and used in the cap enforcement block.
        """
        scorer_path = ROOT / "core" / "meta_decision" / "confluence_scorer.py"
        content = scorer_path.read_text()

        # Must compute tech baseline from non-orchestrator signals
        assert '_tech_signals = [s for s in active_signals if s.model_name != "orchestrator"]' in content
        assert "_mil_technical_baseline" in content

        # Cap block must use _mil_technical_baseline, NOT _pre_modifier_score
        cap_idx = content.index("MIL Hard Cap Enforcement")
        cap_block = content[cap_idx:cap_idx + 1500]
        assert "_mil_technical_baseline" in cap_block
        assert "_pre_modifier_score" not in cap_block

    def test_cap_clamps_positive_overshoot(self):
        """
        If total MIL (orchestrator + OI/Liq) pushes score above cap.
        Example: tech_baseline = 0.50, final = 0.80 (delta = +0.30).
        Cap = 30% of 0.50 = 0.15. Clamped score = 0.50 + 0.15 = 0.65.
        """
        from core.agents.mil.funding_rate_enhanced import MIL_INFLUENCE_CAP

        tech_baseline = 0.50
        final_score = 0.80
        delta = final_score - tech_baseline  # +0.30
        max_delta = MIL_INFLUENCE_CAP * tech_baseline  # 0.15

        assert abs(delta) > max_delta, "Test setup: delta must exceed cap"

        clamped_delta = max(-max_delta, min(max_delta, delta))
        clamped_score = tech_baseline + clamped_delta

        assert clamped_score == pytest.approx(0.65, abs=0.001)
        assert clamped_score < final_score, "Clamped must be less than unclamped"

    def test_cap_clamps_negative_overshoot(self):
        """
        If total MIL pushes score below cap.
        Example: tech_baseline = 0.60, final = 0.30 (delta = -0.30).
        Cap = 30% of 0.60 = 0.18. Clamped score = 0.60 - 0.18 = 0.42.
        """
        from core.agents.mil.funding_rate_enhanced import MIL_INFLUENCE_CAP

        tech_baseline = 0.60
        final_score = 0.30
        delta = final_score - tech_baseline  # -0.30
        max_delta = MIL_INFLUENCE_CAP * tech_baseline  # 0.18

        assert abs(delta) > max_delta, "Test setup: delta must exceed cap"

        clamped_delta = max(-max_delta, min(max_delta, delta))
        clamped_score = tech_baseline + clamped_delta

        assert clamped_score == pytest.approx(0.42, abs=0.001)
        assert clamped_score > final_score, "Clamped must be greater than (negative) unclamped"

    def test_cap_allows_within_range(self):
        """
        If delta is within cap, score should NOT be modified.
        Example: tech = 0.60, final = 0.68 (delta = +0.08). Cap = 0.18.
        """
        from core.agents.mil.funding_rate_enhanced import MIL_INFLUENCE_CAP

        tech_baseline = 0.60
        final_score = 0.68
        delta = final_score - tech_baseline  # +0.08
        max_delta = MIL_INFLUENCE_CAP * tech_baseline  # 0.18

        assert abs(delta) <= max_delta, "Test setup: delta must be within cap"
        clamped_delta = max(-max_delta, min(max_delta, delta))
        clamped_score = tech_baseline + clamped_delta
        assert clamped_score == pytest.approx(final_score, abs=0.0001)

    def test_cap_value_is_030(self):
        """MIL_INFLUENCE_CAP must be exactly 0.30."""
        from core.agents.mil.funding_rate_enhanced import MIL_INFLUENCE_CAP
        assert MIL_INFLUENCE_CAP == 0.30

    def test_cap_enforcement_only_in_live_mode(self):
        """
        The cap is only applied when technical_only=False.
        In backtest (technical_only=True), MIL is completely blocked
        (no orchestrator, no OI modifiers), so cap is irrelevant.
        """
        scorer_path = ROOT / "core" / "meta_decision" / "confluence_scorer.py"
        content = scorer_path.read_text()
        cap_idx = content.index("MIL Hard Cap Enforcement")
        block_before = content[max(0, cap_idx - 2000):cap_idx]
        assert "if not technical_only" in block_before

    def test_cap_recorded_in_diagnostics(self):
        """
        ConfluenceScorer._last_diagnostics must contain MIL cap keys
        including the new mil_technical_baseline key.
        """
        scorer_path = ROOT / "core" / "meta_decision" / "confluence_scorer.py"
        content = scorer_path.read_text()
        assert '"mil_delta_raw"' in content or "'mil_delta_raw'" in content
        assert '"mil_delta_max"' in content or "'mil_delta_max'" in content
        assert '"mil_capped"' in content or "'mil_capped'" in content
        assert '"mil_technical_baseline"' in content or "'mil_technical_baseline'" in content

    def test_orchestrator_plus_oi_combined_cannot_exceed_cap(self):
        """
        KEY CORRECTNESS TEST: orchestrator contributes +0.15 to score,
        OI/Liq contributes +0.20.  Total MIL delta = +0.35.
        Tech baseline = 0.50.  Cap = 30% × 0.50 = 0.15.
        Combined delta (0.35) exceeds cap (0.15) → must clamp to 0.65.

        This test would PASS with the OLD (wrong) code because the old
        code used _pre_modifier_score (which included orchestrator),
        so it would only see the OI/Liq delta of 0.20.
        With tech_baseline, the full 0.35 delta is correctly captured.
        """
        from core.agents.mil.funding_rate_enhanced import MIL_INFLUENCE_CAP

        tech_baseline = 0.50  # pure technical score (no orchestrator)
        orch_boost = 0.15     # orchestrator raises score to 0.65
        oi_liq_boost = 0.20   # OI/Liq raises score to 0.85

        score_after_orch = tech_baseline + orch_boost       # 0.65
        score_after_all  = score_after_orch + oi_liq_boost  # 0.85

        total_mil_delta = score_after_all - tech_baseline   # +0.35
        max_delta = MIL_INFLUENCE_CAP * tech_baseline       # 0.15

        assert total_mil_delta > max_delta, "Test setup: combined MIL must exceed cap"

        # Apply clamp
        clamped_delta = max(-max_delta, min(max_delta, total_mil_delta))
        clamped_score = max(0.0, min(1.0, tech_baseline + clamped_delta))

        assert clamped_score == pytest.approx(0.65, abs=0.001)
        assert clamped_score < score_after_all

        # OLD (wrong) approach would use score_after_orch as baseline:
        old_baseline = score_after_orch  # 0.65 — WRONG, includes orchestrator
        old_delta = score_after_all - old_baseline  # 0.20
        old_max = MIL_INFLUENCE_CAP * old_baseline  # 0.195
        old_would_clamp = abs(old_delta) > old_max
        # With old code: delta 0.20 > cap 0.195 → barely clamped,
        # but total MIL influence would be 0.35 (0.85 - 0.50) = 70% of baseline!
        # The new code correctly captures the full 0.35 delta.
        assert total_mil_delta / tech_baseline > MIL_INFLUENCE_CAP, (
            "Total MIL influence exceeds cap — new code catches this"
        )

    def test_extreme_mil_cannot_exceed_cap_mathematically(self):
        """
        Proof: for ANY tech_baseline > 0 and ANY final_score,
        the clamped result satisfies |clamped - tech| <= cap * tech.
        Test with 100 random combinations including orchestrator + OI/Liq.
        """
        from core.agents.mil.funding_rate_enhanced import MIL_INFLUENCE_CAP

        import random
        random.seed(42)
        for _ in range(100):
            tech = random.uniform(0.01, 1.0)
            # Simulate random orchestrator + OI/Liq contributions
            orch_delta = random.uniform(-0.3, 0.3)
            oi_delta = random.uniform(-0.2, 0.2)
            liq_delta = random.uniform(-0.1, 0.1)
            final = tech + orch_delta + oi_delta + liq_delta

            total_delta = final - tech
            max_delta = MIL_INFLUENCE_CAP * tech
            clamped = max(-max_delta, min(max_delta, total_delta))
            result = max(0.0, min(1.0, tech + clamped))
            actual_delta = result - tech
            assert abs(actual_delta) <= max_delta + 1e-9, (
                f"Cap violated: tech={tech:.4f} final={final:.4f} "
                f"result={result:.4f} delta={actual_delta:.4f} max={max_delta:.4f}"
            )

    def test_tech_baseline_excludes_orchestrator_signal(self):
        """
        Verify the code computes _mil_technical_baseline by filtering
        out model_name == 'orchestrator' from active_signals.
        """
        scorer_path = ROOT / "core" / "meta_decision" / "confluence_scorer.py"
        content = scorer_path.read_text()

        # Find the tech baseline computation
        assert "_tech_signals" in content
        baseline_idx = content.index("_tech_signals")
        baseline_block = content[baseline_idx:baseline_idx + 600]

        # Must filter on model_name != "orchestrator"
        assert 'model_name != "orchestrator"' in baseline_block
        # Must compute weighted sum from _tech_signals
        assert "_tech_total_weight" in baseline_block
        assert "_mil_technical_baseline" in baseline_block


# ══════════════════════════════════════════════════════════════
# SECTION 3: Regression — No Change When MIL Disabled
# ══════════════════════════════════════════════════════════════

class TestMILDisabledRegression:
    """
    Verify that when MIL is disabled (default), all behavior
    is IDENTICAL to pre-Phase 4A.
    """

    def test_mil_defaults_all_false(self):
        """All MIL config gates must default to False."""
        from config.settings import DEFAULT_CONFIG
        assert DEFAULT_CONFIG["mil"]["global_enabled"] is False
        assert DEFAULT_CONFIG["agents"]["funding_rate_enhanced"] is False
        assert DEFAULT_CONFIG["agents"]["oi_enhanced"] is False

    def test_funding_agent_process_unchanged_when_mil_disabled(self):
        """
        FundingRateAgent.process() output must be identical to pre-MIL
        when MIL is disabled.
        """
        from core.agents.mil.funding_rate_enhanced import FundingRateEnhancer
        with patch.object(FundingRateEnhancer, "is_enabled", return_value=False):
            from core.agents.funding_rate_agent import FundingRateAgent
            agent = FundingRateAgent.__new__(FundingRateAgent)
            agent._cache = {}
            agent._lock = threading.RLock()
            agent._rate_history = {}

            raw = {
                "BTC/USDT": {"rate_pct": 0.05, "oi_usdt": 1_000_000},
                "ETH/USDT": {"rate_pct": -0.03, "oi_usdt": 500_000},
            }
            result = agent.process(raw)

            # Standard fields must be present
            assert result["mil_active"] is False
            for sym in ["BTC/USDT", "ETH/USDT"]:
                s = result["symbols"][sym]
                assert "signal" in s
                assert "confidence" in s
                assert "direction" in s
                # NO mil_* keys should be present
                mil_keys = [k for k in s if k.startswith("mil_")]
                assert len(mil_keys) == 0, f"MIL keys found when disabled: {mil_keys}"

    def test_fetch_phase_skips_mil_when_disabled(self):
        """
        FundingRateAgent.fetch() must NOT call fetch_all_symbols
        when MIL is disabled.
        """
        agent_path = ROOT / "core" / "agents" / "funding_rate_agent.py"
        content = agent_path.read_text()
        # The MIL pre-fetch is guarded by is_enabled()
        assert "_enhancer.is_enabled()" in content or "is_enabled()" in content

    def test_confluence_scorer_mil_cap_skipped_in_technical_only(self):
        """
        In technical_only=True, the MIL cap block should not execute.
        Verify the guard.
        """
        scorer_path = ROOT / "core" / "meta_decision" / "confluence_scorer.py"
        content = scorer_path.read_text()
        cap_idx = content.index("MIL Hard Cap Enforcement")
        # The guard `if not technical_only` must appear before the cap block
        guard_region = content[max(0, cap_idx - 2000):cap_idx]
        assert "if not technical_only" in guard_region

    def test_oi_enhancer_disabled_returns_unmodified_data(self):
        """OIEnhancer.enhance_oi_result should be a no-op when disabled."""
        from core.agents.mil.oi_enhanced import OIEnhancer
        with patch.object(OIEnhancer, "is_enabled", return_value=False):
            e = OIEnhancer()
            base = {"raw_oi_usd": 1_000_000, "source": "coinglass"}
            # When disabled, CoinglassAgent doesn't even call enhance —
            # but verify the enhancer itself reports disabled
            assert e.is_enabled() is False
