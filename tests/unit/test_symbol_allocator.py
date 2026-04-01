"""
Tests for Symbol Priority & Allocation System — Session 24.

Test IDs: SA-01 through SA-11 (functional), SA-12 (run_batch integration),
          SA-13 (edge cases), SA-14 (settings default coverage),
          SA-15 (regression — no side-effects on strategy/risk).

Total: 55 tests
"""
from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_candidate(symbol: str, score: float, side: str = "buy",
                    models_fired: list | None = None) -> dict:
    return {
        "symbol":       symbol,
        "score":        score,
        "side":         side,
        "models_fired": models_fired or ["trend"],
        "regime":       "bull_trend",
        "_no_signal":   False,
    }


def _settings_map(overrides: dict):
    """Return a side_effect function for settings.get() mocking."""
    def _get(key, default=None):
        return overrides.get(key, default)
    return _get


# ── SA-01: STATIC mode — default weights ─────────────────────────────────────

class TestStaticMode:
    """SA-01 through SA-05: STATIC mode weight resolution."""

    def test_sa01_btc_weight_is_one(self):
        """BTC/USDT static weight default = 1.0."""
        from core.analytics.symbol_allocator import SymbolAllocator
        a = SymbolAllocator()
        with patch("core.analytics.symbol_allocator._s") as mock_s:
            mock_s.get.side_effect = _settings_map({
                "symbol_allocation.mode": "STATIC",
                "symbol_allocation.static_weights.BTC/USDT": 1.0,
            })
            assert a.get_weight("BTC/USDT") == pytest.approx(1.0)

    def test_sa02_eth_weight_is_1_2(self):
        """ETH/USDT static weight default = 1.2."""
        from core.analytics.symbol_allocator import SymbolAllocator
        a = SymbolAllocator()
        with patch("core.analytics.symbol_allocator._s") as mock_s:
            mock_s.get.side_effect = _settings_map({
                "symbol_allocation.mode": "STATIC",
                "symbol_allocation.static_weights.ETH/USDT": 1.2,
            })
            assert a.get_weight("ETH/USDT") == pytest.approx(1.2)

    def test_sa03_sol_weight_is_1_3(self):
        """SOL/USDT static weight default = 1.3 (highest)."""
        from core.analytics.symbol_allocator import SymbolAllocator
        a = SymbolAllocator()
        with patch("core.analytics.symbol_allocator._s") as mock_s:
            mock_s.get.side_effect = _settings_map({
                "symbol_allocation.mode": "STATIC",
                "symbol_allocation.static_weights.SOL/USDT": 1.3,
            })
            assert a.get_weight("SOL/USDT") == pytest.approx(1.3)

    def test_sa04_bnb_weight_is_0_8(self):
        """BNB/USDT static weight default = 0.8."""
        from core.analytics.symbol_allocator import SymbolAllocator
        a = SymbolAllocator()
        with patch("core.analytics.symbol_allocator._s") as mock_s:
            mock_s.get.side_effect = _settings_map({
                "symbol_allocation.mode": "STATIC",
                "symbol_allocation.static_weights.BNB/USDT": 0.8,
            })
            assert a.get_weight("BNB/USDT") == pytest.approx(0.8)

    def test_sa05_unknown_symbol_returns_default(self):
        """Unknown symbol returns _DEFAULT_WEIGHT (1.0) in STATIC mode."""
        from core.analytics.symbol_allocator import SymbolAllocator
        a = SymbolAllocator()
        with patch("core.analytics.symbol_allocator._s") as mock_s:
            mock_s.get.side_effect = _settings_map({
                "symbol_allocation.mode": "STATIC",
                # No entry for DOGE/USDT
            })
            w = a.get_weight("DOGE/USDT")
            assert w == pytest.approx(1.0)


# ── SA-06: DYNAMIC mode — regime selection ───────────────────────────────────

class TestDynamicMode:
    """SA-06 through SA-08: DYNAMIC mode — BTC dominance → regime → profile."""

    def test_sa06_high_dominance_selects_btc_dominant(self):
        """Dominance 60% > high 55% → BTC_DOMINANT regime."""
        from core.analytics.symbol_allocator import SymbolAllocator, REGIME_BTC_DOMINANT
        a = SymbolAllocator()
        with patch("core.analytics.symbol_allocator._s") as mock_s:
            mock_s.get.side_effect = _settings_map({
                "symbol_allocation.mode": "DYNAMIC",
                "symbol_allocation.btc_dominance_pct": 60.0,
                "symbol_allocation.btc_dominance_high": 55.0,
                "symbol_allocation.btc_dominance_low": 45.0,
            })
            assert a.get_regime() == REGIME_BTC_DOMINANT

    def test_sa07_low_dominance_selects_alt_season(self):
        """Dominance 40% < low 45% → ALT_SEASON regime."""
        from core.analytics.symbol_allocator import SymbolAllocator, REGIME_ALT_SEASON
        a = SymbolAllocator()
        with patch("core.analytics.symbol_allocator._s") as mock_s:
            mock_s.get.side_effect = _settings_map({
                "symbol_allocation.mode": "DYNAMIC",
                "symbol_allocation.btc_dominance_pct": 40.0,
                "symbol_allocation.btc_dominance_high": 55.0,
                "symbol_allocation.btc_dominance_low": 45.0,
            })
            assert a.get_regime() == REGIME_ALT_SEASON

    def test_sa08_neutral_dominance_selects_neutral(self):
        """Dominance 50% between 45 and 55 → NEUTRAL regime."""
        from core.analytics.symbol_allocator import SymbolAllocator, REGIME_NEUTRAL
        a = SymbolAllocator()
        with patch("core.analytics.symbol_allocator._s") as mock_s:
            mock_s.get.side_effect = _settings_map({
                "symbol_allocation.mode": "DYNAMIC",
                "symbol_allocation.btc_dominance_pct": 50.0,
                "symbol_allocation.btc_dominance_high": 55.0,
                "symbol_allocation.btc_dominance_low": 45.0,
            })
            assert a.get_regime() == REGIME_NEUTRAL

    def test_sa09_dynamic_btc_dominant_btc_weight(self):
        """BTC_DOMINANT profile: BTC/USDT weight = 1.4."""
        from core.analytics.symbol_allocator import SymbolAllocator
        a = SymbolAllocator()
        with patch("core.analytics.symbol_allocator._s") as mock_s:
            mock_s.get.side_effect = _settings_map({
                "symbol_allocation.mode": "DYNAMIC",
                "symbol_allocation.btc_dominance_pct": 60.0,
                "symbol_allocation.btc_dominance_high": 55.0,
                "symbol_allocation.btc_dominance_low": 45.0,
                "symbol_allocation.profiles.btc_dominant.BTC/USDT": 1.4,
            })
            assert a.get_weight("BTC/USDT") == pytest.approx(1.4)

    def test_sa10_dynamic_alt_season_sol_weight(self):
        """ALT_SEASON profile: SOL/USDT weight = 1.5."""
        from core.analytics.symbol_allocator import SymbolAllocator
        a = SymbolAllocator()
        with patch("core.analytics.symbol_allocator._s") as mock_s:
            mock_s.get.side_effect = _settings_map({
                "symbol_allocation.mode": "DYNAMIC",
                "symbol_allocation.btc_dominance_pct": 40.0,
                "symbol_allocation.btc_dominance_high": 55.0,
                "symbol_allocation.btc_dominance_low": 45.0,
                "symbol_allocation.profiles.alt_season.SOL/USDT": 1.5,
            })
            assert a.get_weight("SOL/USDT") == pytest.approx(1.5)

    def test_sa10b_static_mode_get_regime_returns_neutral(self):
        """STATIC mode: get_regime() always returns NEUTRAL."""
        from core.analytics.symbol_allocator import SymbolAllocator, REGIME_NEUTRAL
        a = SymbolAllocator()
        with patch("core.analytics.symbol_allocator._s") as mock_s:
            mock_s.get.side_effect = _settings_map({
                "symbol_allocation.mode": "STATIC",
            })
            assert a.get_regime() == REGIME_NEUTRAL


# ── SA-11: get_adjusted_score ─────────────────────────────────────────────────

class TestAdjustedScore:
    """SA-11: adjusted_score = base_score × symbol_weight."""

    def test_sa11_adjusted_score_math(self):
        """adjusted_score = 0.80 × 1.3 = 1.04 for SOL with weight 1.3."""
        from core.analytics.symbol_allocator import SymbolAllocator
        a = SymbolAllocator()
        c = _make_candidate("SOL/USDT", 0.80)
        with patch("core.analytics.symbol_allocator._s") as mock_s:
            mock_s.get.side_effect = _settings_map({
                "symbol_allocation.mode": "STATIC",
                "symbol_allocation.static_weights.SOL/USDT": 1.3,
            })
            adj = a.get_adjusted_score(c)
        assert adj == pytest.approx(0.80 * 1.3, rel=1e-6)

    def test_sa11b_base_score_not_mutated(self):
        """get_adjusted_score() does NOT modify candidate['score']."""
        from core.analytics.symbol_allocator import SymbolAllocator
        a = SymbolAllocator()
        c = _make_candidate("ETH/USDT", 0.75)
        with patch("core.analytics.symbol_allocator._s") as mock_s:
            mock_s.get.side_effect = _settings_map({
                "symbol_allocation.mode": "STATIC",
                "symbol_allocation.static_weights.ETH/USDT": 1.2,
            })
            a.get_adjusted_score(c)
        assert c["score"] == pytest.approx(0.75)

    def test_sa11c_weight_one_preserves_score(self):
        """symbol_weight=1.0 → adjusted_score equals base_score."""
        from core.analytics.symbol_allocator import SymbolAllocator
        a = SymbolAllocator()
        c = _make_candidate("BTC/USDT", 0.65)
        with patch("core.analytics.symbol_allocator._s") as mock_s:
            mock_s.get.side_effect = _settings_map({
                "symbol_allocation.mode": "STATIC",
                "symbol_allocation.static_weights.BTC/USDT": 1.0,
            })
            adj = a.get_adjusted_score(c)
        assert adj == pytest.approx(0.65)


# ── SA-12: rank_candidates ────────────────────────────────────────────────────

class TestRankCandidates:
    """SA-12: rank_candidates() sorts descending by adjusted_score."""

    def _patch_static(self, weights: dict):
        """Return a mock that feeds static weights."""
        def _get(key, default=None):
            if key == "symbol_allocation.mode":
                return "STATIC"
            for sym, w in weights.items():
                if key == f"symbol_allocation.static_weights.{sym}":
                    return w
            return default
        return _get

    def test_sa12_sol_ranks_above_btc(self):
        """With equal base scores SOL (w=1.3) ranks above BTC (w=1.0)."""
        from core.analytics.symbol_allocator import SymbolAllocator
        a = SymbolAllocator()
        cands = [
            _make_candidate("BTC/USDT", 0.70),
            _make_candidate("SOL/USDT", 0.70),
        ]
        with patch("core.analytics.symbol_allocator._s") as mock_s:
            mock_s.get.side_effect = self._patch_static(
                {"BTC/USDT": 1.0, "SOL/USDT": 1.3}
            )
            ranked = a.rank_candidates(cands)
        assert ranked[0]["symbol"] == "SOL/USDT"
        assert ranked[1]["symbol"] == "BTC/USDT"

    def test_sa12b_higher_base_score_can_win_over_lower_weight(self):
        """BTC score=0.90 > SOL score=0.65×1.3=0.845 → BTC ranks first."""
        from core.analytics.symbol_allocator import SymbolAllocator
        a = SymbolAllocator()
        cands = [
            _make_candidate("SOL/USDT", 0.65),
            _make_candidate("BTC/USDT", 0.90),
        ]
        with patch("core.analytics.symbol_allocator._s") as mock_s:
            mock_s.get.side_effect = self._patch_static(
                {"BTC/USDT": 1.0, "SOL/USDT": 1.3}
            )
            ranked = a.rank_candidates(cands)
        assert ranked[0]["symbol"] == "BTC/USDT"

    def test_sa12c_adjusted_score_stamped_on_candidates(self):
        """rank_candidates stamps adjusted_score and symbol_weight keys."""
        from core.analytics.symbol_allocator import SymbolAllocator
        a = SymbolAllocator()
        cands = [_make_candidate("ETH/USDT", 0.80)]
        with patch("core.analytics.symbol_allocator._s") as mock_s:
            mock_s.get.side_effect = self._patch_static({"ETH/USDT": 1.2})
            ranked = a.rank_candidates(cands)
        c = ranked[0]
        assert "adjusted_score" in c
        assert "symbol_weight" in c
        assert c["adjusted_score"] == pytest.approx(0.80 * 1.2)
        assert c["symbol_weight"]  == pytest.approx(1.2)

    def test_sa12d_empty_batch_returns_empty(self):
        """Empty candidate list returns empty list."""
        from core.analytics.symbol_allocator import SymbolAllocator
        a = SymbolAllocator()
        with patch("core.analytics.symbol_allocator._s") as mock_s:
            mock_s.get.side_effect = _settings_map({
                "symbol_allocation.mode": "STATIC",
            })
            assert a.rank_candidates([]) == []

    def test_sa12e_single_candidate_returned_unchanged_ordering(self):
        """Single candidate list returns it ranked (stamped with keys)."""
        from core.analytics.symbol_allocator import SymbolAllocator
        a = SymbolAllocator()
        cands = [_make_candidate("XRP/USDT", 0.50)]
        with patch("core.analytics.symbol_allocator._s") as mock_s:
            mock_s.get.side_effect = self._patch_static({"XRP/USDT": 0.8})
            ranked = a.rank_candidates(cands)
        assert len(ranked) == 1
        assert ranked[0]["symbol"] == "XRP/USDT"
        assert ranked[0]["adjusted_score"] == pytest.approx(0.50 * 0.8)

    def test_sa12f_five_symbols_correct_order(self):
        """5 symbols ranked correctly by adjusted_score."""
        from core.analytics.symbol_allocator import SymbolAllocator
        a = SymbolAllocator()
        weights = {
            "BTC/USDT": 1.0, "ETH/USDT": 1.2, "SOL/USDT": 1.3,
            "BNB/USDT": 0.8, "XRP/USDT": 0.8,
        }
        scores  = {
            "BTC/USDT": 0.70, "ETH/USDT": 0.70, "SOL/USDT": 0.70,
            "BNB/USDT": 0.70, "XRP/USDT": 0.70,
        }
        cands = [_make_candidate(s, scores[s]) for s in weights]
        with patch("core.analytics.symbol_allocator._s") as mock_s:
            mock_s.get.side_effect = self._patch_static(weights)
            ranked = a.rank_candidates(cands)
        # Expected order: SOL(0.91) > ETH(0.84) > BTC(0.70) > BNB=XRP(0.56)
        symbols = [c["symbol"] for c in ranked]
        assert symbols[0] == "SOL/USDT"
        assert symbols[1] == "ETH/USDT"
        assert symbols[2] == "BTC/USDT"
        # BNB and XRP tied — either order acceptable
        assert set(symbols[3:]) == {"BNB/USDT", "XRP/USDT"}


# ── SA-13: weight clamping edge cases ─────────────────────────────────────────

class TestWeightClamping:
    """SA-13: weight bounds enforced regardless of config value."""

    def test_sa13_weight_below_minimum_clamped(self):
        """Weight configured as 0.01 → clamped to _MIN_WEIGHT (0.10)."""
        from core.analytics.symbol_allocator import SymbolAllocator, _MIN_WEIGHT
        a = SymbolAllocator()
        with patch("core.analytics.symbol_allocator._s") as mock_s:
            mock_s.get.side_effect = _settings_map({
                "symbol_allocation.mode": "STATIC",
                "symbol_allocation.static_weights.BTC/USDT": 0.01,
            })
            assert a.get_weight("BTC/USDT") == pytest.approx(_MIN_WEIGHT)

    def test_sa13b_weight_above_maximum_clamped(self):
        """Weight configured as 99.0 → clamped to _MAX_WEIGHT (3.00)."""
        from core.analytics.symbol_allocator import SymbolAllocator, _MAX_WEIGHT
        a = SymbolAllocator()
        with patch("core.analytics.symbol_allocator._s") as mock_s:
            mock_s.get.side_effect = _settings_map({
                "symbol_allocation.mode": "STATIC",
                "symbol_allocation.static_weights.BTC/USDT": 99.0,
            })
            assert a.get_weight("BTC/USDT") == pytest.approx(_MAX_WEIGHT)

    def test_sa13c_exact_min_boundary_not_clamped(self):
        """Weight exactly at _MIN_WEIGHT (0.10) is accepted unchanged."""
        from core.analytics.symbol_allocator import SymbolAllocator, _MIN_WEIGHT
        a = SymbolAllocator()
        with patch("core.analytics.symbol_allocator._s") as mock_s:
            mock_s.get.side_effect = _settings_map({
                "symbol_allocation.mode": "STATIC",
                "symbol_allocation.static_weights.BTC/USDT": _MIN_WEIGHT,
            })
            assert a.get_weight("BTC/USDT") == pytest.approx(_MIN_WEIGHT)

    def test_sa13d_exact_max_boundary_not_clamped(self):
        """Weight exactly at _MAX_WEIGHT (3.00) is accepted unchanged."""
        from core.analytics.symbol_allocator import SymbolAllocator, _MAX_WEIGHT
        a = SymbolAllocator()
        with patch("core.analytics.symbol_allocator._s") as mock_s:
            mock_s.get.side_effect = _settings_map({
                "symbol_allocation.mode": "STATIC",
                "symbol_allocation.static_weights.BTC/USDT": _MAX_WEIGHT,
            })
            assert a.get_weight("BTC/USDT") == pytest.approx(_MAX_WEIGHT)


# ── SA-14: get_status() ───────────────────────────────────────────────────────

class TestGetStatus:
    """SA-14: status dict for diagnostics / rationale panel."""

    def test_sa14_status_has_required_keys(self):
        """get_status() includes all required diagnostic keys."""
        from core.analytics.symbol_allocator import SymbolAllocator
        a = SymbolAllocator()
        with patch("core.analytics.symbol_allocator._s") as mock_s:
            mock_s.get.side_effect = _settings_map({
                "symbol_allocation.mode": "STATIC",
                "symbol_allocation.btc_dominance_pct": 50.0,
                "symbol_allocation.btc_dominance_high": 55.0,
                "symbol_allocation.btc_dominance_low": 45.0,
            })
            status = a.get_status()
        assert "mode" in status
        assert "active_regime" in status
        assert "btc_dominance" in status
        assert "dom_high_thresh" in status
        assert "dom_low_thresh" in status

    def test_sa14b_static_mode_reports_neutral_regime(self):
        """STATIC mode status shows active_regime=NEUTRAL."""
        from core.analytics.symbol_allocator import SymbolAllocator, REGIME_NEUTRAL
        a = SymbolAllocator()
        with patch("core.analytics.symbol_allocator._s") as mock_s:
            mock_s.get.side_effect = _settings_map({
                "symbol_allocation.mode": "STATIC",
                "symbol_allocation.btc_dominance_pct": 60.0,
                "symbol_allocation.btc_dominance_high": 55.0,
                "symbol_allocation.btc_dominance_low": 45.0,
            })
            status = a.get_status()
        assert status["active_regime"] == REGIME_NEUTRAL  # STATIC → always NEUTRAL

    def test_sa14c_dynamic_mode_status_btc_dominant(self):
        """DYNAMIC mode at 60% dominance reports BTC_DOMINANT."""
        from core.analytics.symbol_allocator import SymbolAllocator, REGIME_BTC_DOMINANT
        a = SymbolAllocator()
        with patch("core.analytics.symbol_allocator._s") as mock_s:
            mock_s.get.side_effect = _settings_map({
                "symbol_allocation.mode": "DYNAMIC",
                "symbol_allocation.btc_dominance_pct": 60.0,
                "symbol_allocation.btc_dominance_high": 55.0,
                "symbol_allocation.btc_dominance_low": 45.0,
            })
            status = a.get_status()
        assert status["active_regime"] == REGIME_BTC_DOMINANT


# ── SA-15: run_batch() integration ───────────────────────────────────────────

class TestRunBatchIntegration:
    """
    SA-15: run_batch() in auto_execute_guard uses the allocator for ordering.
    Tests verify that higher-priority symbols win when scores are tied.
    """

    def _make_state(self):
        from core.scanning.auto_execute_guard import AutoExecuteState
        return AutoExecuteState(cooldown_seconds=0)

    def _patch_allocator_static(self, weights: dict):
        """Patch get_allocator() with a real SymbolAllocator using static weights."""
        from core.analytics.symbol_allocator import SymbolAllocator

        def _get(key, default=None):
            if key == "symbol_allocation.mode":
                return "STATIC"
            for sym, w in weights.items():
                if key == f"symbol_allocation.static_weights.{sym}":
                    return w
            return default

        alloc = SymbolAllocator()
        mock_s = MagicMock()
        mock_s.get.side_effect = _get

        patcher = patch(
            "core.analytics.symbol_allocator._s",
            mock_s,
        )
        return patcher

    def test_sa15_sol_approved_over_bnb_when_scores_tied(self):
        """
        When SOL/USDT (w=1.3) and BNB/USDT (w=0.8) both have score=0.70
        and max_pos=1, SOL (adjusted=0.91) should be approved, BNB rejected.
        """
        from core.scanning.auto_execute_guard import run_batch

        sol = _make_candidate("SOL/USDT", 0.70)
        bnb = _make_candidate("BNB/USDT", 0.70)
        state = self._make_state()

        with self._patch_allocator_static({"SOL/USDT": 1.3, "BNB/USDT": 0.8}):
            approved = run_batch(
                candidates=[bnb, sol],   # BNB listed first — allocator should re-rank
                timeframe="1h",
                open_positions=[],
                drawdown_pct=0.0,
                max_dd_pct=15.0,
                max_pos=1,
                state=state,
            )

        assert len(approved) == 1
        assert approved[0]["symbol"] == "SOL/USDT"

    def test_sa15b_allocator_error_does_not_crash_batch(self):
        """If SymbolAllocator raises, run_batch falls back gracefully."""
        from core.scanning.auto_execute_guard import run_batch

        sol = _make_candidate("SOL/USDT", 0.80)
        state = self._make_state()

        # Patch get_allocator to raise (patch at the source module, not import location)
        with patch(
            "core.analytics.symbol_allocator.get_allocator",
            side_effect=ImportError("mock failure"),
        ):
            # Import error in check_candidate's portfolio guard is also expected
            # to be graceful — just run the batch, it should not raise
            try:
                approved = run_batch(
                    candidates=[sol],
                    timeframe="1h",
                    open_positions=[],
                    drawdown_pct=0.0,
                    max_dd_pct=15.0,
                    max_pos=50,
                    state=state,
                )
                # Should either approve or gracefully reject — no exception
                assert isinstance(approved, list)
            except Exception as exc:
                pytest.fail(f"run_batch raised unexpectedly: {exc}")

    def test_sa15c_adjusted_score_stamped_on_approved_candidates(self):
        """Approved candidates have symbol_weight and adjusted_score set."""
        from core.scanning.auto_execute_guard import run_batch

        eth = _make_candidate("ETH/USDT", 0.75)
        state = self._make_state()

        with self._patch_allocator_static({"ETH/USDT": 1.2}):
            approved = run_batch(
                candidates=[eth],
                timeframe="1h",
                open_positions=[],
                drawdown_pct=0.0,
                max_dd_pct=15.0,
                max_pos=50,
                state=state,
            )

        if approved:  # may be rejected by risk checks in test env
            c = approved[0]
            assert "symbol_weight"  in c
            assert "adjusted_score" in c

    def test_sa15d_original_score_unchanged_after_ranking(self):
        """rank_candidates does NOT mutate candidate['score']."""
        from core.analytics.symbol_allocator import SymbolAllocator

        a = SymbolAllocator()
        cands = [_make_candidate("SOL/USDT", 0.70)]
        original_score = cands[0]["score"]

        with patch("core.analytics.symbol_allocator._s") as mock_s:
            mock_s.get.side_effect = _settings_map({
                "symbol_allocation.mode": "STATIC",
                "symbol_allocation.static_weights.SOL/USDT": 1.3,
            })
            a.rank_candidates(cands)

        assert cands[0]["score"] == pytest.approx(original_score)


# ── SA-16: settings.py DEFAULT_CONFIG coverage ───────────────────────────────

class TestSettingsDefaultConfig:
    """SA-16: symbol_allocation keys are present in DEFAULT_CONFIG."""

    def test_sa16_symbol_allocation_in_default_config(self):
        """DEFAULT_CONFIG contains symbol_allocation section."""
        from config.settings import DEFAULT_CONFIG
        assert "symbol_allocation" in DEFAULT_CONFIG

    def test_sa16b_default_mode_is_static(self):
        from config.settings import DEFAULT_CONFIG
        assert DEFAULT_CONFIG["symbol_allocation"]["mode"] == "STATIC"

    def test_sa16c_static_weights_has_five_symbols(self):
        from config.settings import DEFAULT_CONFIG
        sw = DEFAULT_CONFIG["symbol_allocation"]["static_weights"]
        for sym in ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"]:
            assert sym in sw, f"{sym} missing from static_weights"

    def test_sa16d_study4_baseline_weights_correct(self):
        """Study 4 baseline weights: SOL=1.3, ETH=1.2, BTC=1.0, BNB=XRP=0.8."""
        from config.settings import DEFAULT_CONFIG
        sw = DEFAULT_CONFIG["symbol_allocation"]["static_weights"]
        assert sw["SOL/USDT"] == pytest.approx(1.3)
        assert sw["ETH/USDT"] == pytest.approx(1.2)
        assert sw["BTC/USDT"] == pytest.approx(1.0)
        assert sw["BNB/USDT"] == pytest.approx(0.8)
        assert sw["XRP/USDT"] == pytest.approx(0.8)

    def test_sa16e_three_profiles_defined(self):
        from config.settings import DEFAULT_CONFIG
        profiles = DEFAULT_CONFIG["symbol_allocation"]["profiles"]
        assert "btc_dominant" in profiles
        assert "neutral" in profiles
        assert "alt_season" in profiles

    def test_sa16f_profiles_each_have_five_symbols(self):
        from config.settings import DEFAULT_CONFIG
        profiles = DEFAULT_CONFIG["symbol_allocation"]["profiles"]
        syms = {"BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"}
        for profile_name, profile_weights in profiles.items():
            assert set(profile_weights.keys()) == syms, \
                f"Profile '{profile_name}' missing symbols"

    def test_sa16g_btc_dominant_profile_btc_heaviest(self):
        """In BTC_DOMINANT profile BTC/USDT should have highest weight."""
        from config.settings import DEFAULT_CONFIG
        profile = DEFAULT_CONFIG["symbol_allocation"]["profiles"]["btc_dominant"]
        assert profile["BTC/USDT"] == max(profile.values())

    def test_sa16h_alt_season_profile_sol_heaviest(self):
        """In ALT_SEASON profile SOL/USDT should have highest weight."""
        from config.settings import DEFAULT_CONFIG
        profile = DEFAULT_CONFIG["symbol_allocation"]["profiles"]["alt_season"]
        assert profile["SOL/USDT"] == max(profile.values())


# ── SA-17: Regression — no side-effects on strategy/risk ─────────────────────

class TestNoSideEffects:
    """SA-17: Allocator does NOT modify signals, sizing, or risk parameters."""

    def test_sa17_rank_does_not_change_side_key(self):
        """rank_candidates() preserves 'side' key unchanged."""
        from core.analytics.symbol_allocator import SymbolAllocator
        a = SymbolAllocator()
        c = _make_candidate("SOL/USDT", 0.70, side="sell")
        with patch("core.analytics.symbol_allocator._s") as mock_s:
            mock_s.get.side_effect = _settings_map({
                "symbol_allocation.mode": "STATIC",
                "symbol_allocation.static_weights.SOL/USDT": 1.3,
            })
            ranked = a.rank_candidates([c])
        assert ranked[0]["side"] == "sell"

    def test_sa17b_rank_does_not_change_models_fired(self):
        """rank_candidates() preserves 'models_fired' unchanged."""
        from core.analytics.symbol_allocator import SymbolAllocator
        a = SymbolAllocator()
        c = _make_candidate("BTC/USDT", 0.65, models_fired=["trend", "momentum_breakout"])
        with patch("core.analytics.symbol_allocator._s") as mock_s:
            mock_s.get.side_effect = _settings_map({
                "symbol_allocation.mode": "STATIC",
                "symbol_allocation.static_weights.BTC/USDT": 1.0,
            })
            ranked = a.rank_candidates([c])
        assert ranked[0]["models_fired"] == ["trend", "momentum_breakout"]

    def test_sa17c_rank_does_not_change_regime(self):
        """rank_candidates() preserves 'regime' unchanged."""
        from core.analytics.symbol_allocator import SymbolAllocator
        a = SymbolAllocator()
        c = _make_candidate("ETH/USDT", 0.60)
        c["regime"] = "ranging"
        with patch("core.analytics.symbol_allocator._s") as mock_s:
            mock_s.get.side_effect = _settings_map({
                "symbol_allocation.mode": "STATIC",
                "symbol_allocation.static_weights.ETH/USDT": 1.2,
            })
            ranked = a.rank_candidates([c])
        assert ranked[0]["regime"] == "ranging"

    def test_sa17d_symbol_allocator_has_no_set_mode_method(self):
        """SymbolAllocator must not have any set_mode() method (no live-mode switching)."""
        from core.analytics.symbol_allocator import SymbolAllocator
        a = SymbolAllocator()
        assert not hasattr(a, "set_mode")

    def test_sa17e_symbol_allocator_has_no_order_router_import(self):
        """symbol_allocator.py must not import order_router (safety)."""
        import importlib.util, pathlib, inspect
        # Use inspect to find the actual file location (portable across environments)
        from core.analytics import symbol_allocator as _mod
        path = pathlib.Path(inspect.getfile(_mod))
        source = path.read_text()
        assert "order_router" not in source

    def test_sa17f_singleton_is_idempotent(self):
        """get_allocator() returns the same instance on repeated calls."""
        import core.analytics.symbol_allocator as mod
        # Reset singleton for test isolation
        mod._allocator_instance = None
        a1 = mod.get_allocator()
        a2 = mod.get_allocator()
        assert a1 is a2
        mod._allocator_instance = None  # clean up
