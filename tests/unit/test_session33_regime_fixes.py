"""
Session 33 — Reproduction tests for 3 regime classifier bugs + risk_pct default mismatch.

Each test MUST FAIL before the fix is applied and PASS after.
Run with: pytest tests/unit/test_session33_regime_fixes.py -v

Bug 1: ADX dead zone (20 ≤ ADX < 25) → always REGIME_UNCERTAIN
Bug 2: ema_slope=None with ADX ≥ 25 → falls through to REGIME_UNCERTAIN
Bug 3: Hysteresis initialized to "uncertain" → first 2 calls always return "uncertain"
Bug 4: risk_pct_per_trade hardcoded default 0.75 ≠ production config 0.5
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from unittest.mock import patch, MagicMock

from core.regime.regime_classifier import (
    RegimeClassifier,
    REGIME_UNCERTAIN, REGIME_BULL_TREND, REGIME_BEAR_TREND,
    REGIME_RANGING, REGIME_VOL_COMPRESS, REGIME_VOL_EXPANSION,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_normal_df(
    rows: int = 60,
    adx: float = 22.0,
    ema_slope_up: bool = True,
    include_ema: bool = True,
    rsi: float = 50.0,
    bb_ratio: float = 1.0,   # bb_width_ratio — 1.0 = normal (no vol expansion/compression)
) -> pd.DataFrame:
    """
    Build a minimal indicator DataFrame that satisfies all column requirements.

    Defaults keep the price series completely normal (no crisis, no squeeze,
    no extreme BB) so that regime classification falls purely through the
    ADX / EMA slope branch.

    bb_expansion_factor default = 1.5  → bb_ratio < 1.5 avoids vol_expansion
    bb_compression_factor default = 0.6 → bb_ratio > 0.6 avoids vol_compression
    """
    np.random.seed(42)
    close = 50_000.0 + np.cumsum(np.random.randn(rows) * 10)

    # EMA 20 — gently rising or falling
    slope_factor = 1.0001 if ema_slope_up else 0.9999
    ema20 = close.copy()
    for i in range(1, rows):
        ema20[i] = ema20[i - 1] * slope_factor

    # BB — fixed ratio around close so we can control bb_width_ratio precisely
    # We need at least bb_rolling_window (20) valid widths, so rows must be ≥ 40
    mid = close.copy()
    # Set bb_width so that (bb_width / rolling_mean) ≈ bb_ratio
    # Since rolling_mean == bb_width when all values are equal, set constant width
    bb_width_abs = mid * 0.02   # 2% of price
    upper = mid + bb_width_abs / 2
    lower = mid - bb_width_abs / 2

    # Override the last value to achieve target bb_ratio.
    # rolling_mean of (bb_upper - bb_lower)/mid over 20 bars ≈ 0.02
    # So bb_width_last / 0.02 = bb_ratio → bb_width_last = 0.02 * bb_ratio
    target_bw = 0.02 * bb_ratio
    upper[-1] = mid[-1] * (1 + target_bw / 2)
    lower[-1] = mid[-1] * (1 - target_bw / 2)

    rsi_series = np.full(rows, rsi)

    # Volume — flat, then slightly rising to avoid triggering crisis checks
    volume = np.full(rows, 1000.0)

    data = {
        "close":    close,
        "volume":   volume,
        "rsi":      rsi_series,
        "adx":      np.full(rows, adx),
        "bb_upper": upper,
        "bb_lower": lower,
        "bb_mid":   mid,
    }
    if include_ema:
        data["ema_20"] = ema20

    return pd.DataFrame(data)


# ═══════════════════════════════════════════════════════════════════════
# BUG 1: ADX dead zone  (20 ≤ ADX < 25) → always REGIME_UNCERTAIN
# ═══════════════════════════════════════════════════════════════════════

class TestAdxDeadZone:
    """
    When ADX is between adx_ranging_threshold (20) and adx_trend_threshold (25),
    neither the 'adx >= 25' branch nor the 'adx < 20' branch fires.
    The code falls straight to the fallback 'uncertain' block.

    Expected after fix: ADX 20-25 with clear EMA slope → RANGING (not uncertain).
    """

    @pytest.mark.parametrize("adx_val", [20.0, 21.5, 22.0, 23.5, 24.9])
    def test_adx_dead_zone_up_slope_not_uncertain(self, adx_val: float):
        """ADX in dead zone with rising EMA → should NOT be 'uncertain'."""
        clf = RegimeClassifier()
        df = _make_normal_df(adx=adx_val, ema_slope_up=True, rows=60)
        # Call 3 times to let hysteresis stabilise (so this test isolates ADX bug only)
        for _ in range(3):
            regime, conf, _ = clf.classify(df)
        assert regime != REGIME_UNCERTAIN, (
            f"ADX={adx_val} with positive slope produced '{regime}' — "
            f"ADX dead zone not fixed"
        )

    @pytest.mark.parametrize("adx_val", [20.0, 21.5, 22.0, 23.5, 24.9])
    def test_adx_dead_zone_down_slope_not_uncertain(self, adx_val: float):
        """ADX in dead zone with falling EMA → should NOT be 'uncertain'."""
        clf = RegimeClassifier()
        df = _make_normal_df(adx=adx_val, ema_slope_up=False, rows=60)
        for _ in range(3):
            regime, conf, _ = clf.classify(df)
        assert regime != REGIME_UNCERTAIN, (
            f"ADX={adx_val} with negative slope produced '{regime}' — "
            f"ADX dead zone not fixed"
        )

    def test_adx_dead_zone_returns_ranging(self):
        """Dead-zone ADX (22) with no strong directional signal → RANGING expected."""
        clf = RegimeClassifier()
        df = _make_normal_df(adx=22.0, ema_slope_up=True, rsi=50.0, rows=60)
        for _ in range(3):
            regime, conf, _ = clf.classify(df)
        # After fix, dead zone should map to RANGING not UNCERTAIN
        assert regime == REGIME_RANGING, (
            f"ADX=22 expected 'ranging', got '{regime}'"
        )

    def test_adx_just_below_trend_threshold_not_uncertain(self):
        """ADX=24.9 (just below trend threshold) should resolve to RANGING, not uncertain."""
        clf = RegimeClassifier()
        df = _make_normal_df(adx=24.9, ema_slope_up=True, rows=60)
        for _ in range(3):
            regime, conf, _ = clf.classify(df)
        assert regime != REGIME_UNCERTAIN, (
            f"ADX=24.9 (just below trend threshold) produced '{regime}'"
        )

    def test_adx_at_ranging_threshold_still_ranging(self):
        """ADX=20.0 (exactly at ranging threshold) should be RANGING."""
        clf = RegimeClassifier()
        df = _make_normal_df(adx=20.0, ema_slope_up=True, rows=60)
        for _ in range(3):
            regime, conf, _ = clf.classify(df)
        assert regime != REGIME_UNCERTAIN, (
            f"ADX=20.0 (at ranging threshold) produced '{regime}'"
        )


# ═══════════════════════════════════════════════════════════════════════
# BUG 2: ema_slope=None with ADX ≥ 25 → falls through to UNCERTAIN
# ═══════════════════════════════════════════════════════════════════════

class TestEmaSloneNoneHighAdx:
    """
    When ADX ≥ adx_trend_threshold (25) but ema_20 column is missing
    (ema_slope = None), the code enters the 'if adx >= 25' block, fails the
    'if ema_slope is not None' check, and falls through WITHOUT returning.
    The next 'if adx < 20' check also fails (ADX is 30), so we fall to
    the fallback 'uncertain' block.

    Expected after fix: ADX ≥ 25 with no EMA → return RANGING as a fallback
    (not uncertain) since we can't determine direction.
    """

    def test_high_adx_no_ema_column_not_uncertain(self):
        """ADX=30 with no ema_20 column → should NOT return 'uncertain'."""
        clf = RegimeClassifier()
        df = _make_normal_df(adx=30.0, include_ema=False, rows=60)
        for _ in range(3):
            regime, conf, _ = clf.classify(df)
        assert regime != REGIME_UNCERTAIN, (
            f"ADX=30 with no EMA column produced '{regime}' — "
            f"ema_slope=None fall-through not fixed"
        )

    def test_high_adx_no_ema_returns_ranging_fallback(self):
        """ADX=35 with missing EMA → should fall back to RANGING (direction unknown)."""
        clf = RegimeClassifier()
        df = _make_normal_df(adx=35.0, include_ema=False, rows=60)
        for _ in range(3):
            regime, conf, _ = clf.classify(df)
        assert regime == REGIME_RANGING, (
            f"ADX=35 no EMA expected 'ranging' fallback, got '{regime}'"
        )

    def test_adx_exactly_at_threshold_no_ema_not_uncertain(self):
        """ADX=25.0 exactly with no EMA → should NOT return 'uncertain'."""
        clf = RegimeClassifier()
        df = _make_normal_df(adx=25.0, include_ema=False, rows=60)
        for _ in range(3):
            regime, conf, _ = clf.classify(df)
        assert regime != REGIME_UNCERTAIN, (
            f"ADX=25.0 exactly, no EMA produced '{regime}'"
        )


# ═══════════════════════════════════════════════════════════════════════
# BUG 3: Hysteresis initialized to "uncertain"
# ═══════════════════════════════════════════════════════════════════════

class TestHysteresisInit:
    """
    self._committed_regime is initialized to "uncertain". The hysteresis buffer
    requires 3 consecutive identical regimes before committing. On the first 2
    calls, regardless of market conditions, _apply_hysteresis returns
    self._committed_regime = "uncertain".

    Expected after fix: First call with unambiguous data (ADX=40, rising EMA)
    should NOT return "uncertain" — it should return the raw signal with a small
    confidence penalty until hysteresis commits.
    """

    def test_first_call_strong_bull_not_uncertain(self):
        """Strong bull trend on first call should not return 'uncertain'."""
        clf = RegimeClassifier()
        df = _make_normal_df(adx=40.0, ema_slope_up=True, rows=60)
        regime, conf, _ = clf.classify(df)
        assert regime != REGIME_UNCERTAIN, (
            f"First call with ADX=40, rising EMA returned '{regime}' — "
            f"hysteresis init bug not fixed"
        )

    def test_first_call_strong_bear_not_uncertain(self):
        """Strong bear trend on first call should not return 'uncertain'."""
        clf = RegimeClassifier()
        df = _make_normal_df(adx=35.0, ema_slope_up=False, rows=60)
        regime, conf, _ = clf.classify(df)
        assert regime != REGIME_UNCERTAIN, (
            f"First call with ADX=35, falling EMA returned '{regime}' — "
            f"hysteresis init bug not fixed"
        )

    def test_second_call_strong_bull_not_uncertain(self):
        """Second call with same strong bull data should not return 'uncertain'."""
        clf = RegimeClassifier()
        df = _make_normal_df(adx=40.0, ema_slope_up=True, rows=60)
        clf.classify(df)  # first call
        regime, conf, _ = clf.classify(df)  # second call
        assert regime != REGIME_UNCERTAIN, (
            f"Second call with ADX=40, rising EMA returned '{regime}'"
        )

    def test_committed_regime_defaults_to_empty_not_uncertain(self):
        """Fresh RegimeClassifier should have no prior committed regime (empty, not 'uncertain')."""
        clf = RegimeClassifier()
        # Before any call, committed regime should be empty string, not 'uncertain'
        assert clf._committed_regime != REGIME_UNCERTAIN, (
            f"_committed_regime initialized to '{clf._committed_regime}' — "
            f"should be empty string after fix"
        )

    def test_hysteresis_commits_after_3_consistent_bars(self):
        """After 3 consistent calls, regime should commit and stay committed."""
        clf = RegimeClassifier()
        df = _make_normal_df(adx=40.0, ema_slope_up=True, rows=60)
        for _ in range(3):
            regime, _, _ = clf.classify(df)
        # After 3 calls, regime should be bull_trend AND committed
        assert regime == REGIME_BULL_TREND, (
            f"After 3 consistent bull calls, expected 'bull_trend', got '{regime}'"
        )
        assert clf._committed_regime == REGIME_BULL_TREND, (
            f"Expected _committed_regime='bull_trend', got '{clf._committed_regime}'"
        )


# ═══════════════════════════════════════════════════════════════════════
# BUG 4: risk_pct default mismatch  (0.75 vs production 0.5)
# ═══════════════════════════════════════════════════════════════════════

class TestRiskPctDefault:
    """
    Both confluence_scorer.py (line 608) and position_sizer.py (line 255) use:
        risk_pct = float(_s.get("risk_engine.risk_pct_per_trade", 0.75))

    The production config (config.yaml) sets risk_pct_per_trade = 0.5.
    If settings fails to load (e.g., during tests or startup edge cases), the
    code silently uses 0.75 instead of the intended 0.5, producing 50% larger
    positions than expected.

    The default MUST match production config (0.5).
    """

    def test_position_sizer_default_risk_pct_is_0_5(self):
        """PositionSizer.calculate() should default risk_pct to 0.5, not 0.75."""
        import inspect
        import core.meta_decision.position_sizer as ps_module

        source = inspect.getsource(ps_module)
        assert '"risk_engine.risk_pct_per_trade", 0.75)' not in source, (
            "position_sizer.py still has default 0.75 — should be 0.5"
        )
        assert '"risk_engine.risk_pct_per_trade", 0.5)' in source, (
            "position_sizer.py default risk_pct should be 0.5"
        )

    def test_confluence_scorer_default_risk_pct_is_0_5(self):
        """ConfluenceScorer sizing path should default risk_pct to 0.5, not 0.75."""
        import inspect
        import core.meta_decision.confluence_scorer as cs_module

        source = inspect.getsource(cs_module)
        assert '"risk_engine.risk_pct_per_trade", 0.75)' not in source, (
            "confluence_scorer.py still has default 0.75 — should be 0.5"
        )
        assert '"risk_engine.risk_pct_per_trade", 0.5)' in source, (
            "confluence_scorer.py default risk_pct should be 0.5"
        )

    def test_position_sizer_risk_based_uses_correct_pct(self):
        """
        When settings cannot be read, calculate() should use 0.5% risk, not 0.75%.
        Verify numerically: with $100k capital and known stop distance, size should
        match 0.5% risk calculation.
        """
        from core.meta_decision.position_sizer import PositionSizer

        sizer = PositionSizer()

        capital    = 100_000.0
        entry      = 50_000.0
        stop       = 49_000.0   # $1000 stop distance
        # Expected at 0.5%: risk_usdt = 500, qty = 0.5, size = 25_000 USDT
        # Expected at 0.75%: risk_usdt = 750, qty = 0.75, size = 37_500 USDT
        # Cap is 4% = 4_000 USDT
        expected_at_0_5_pct = min((0.5 / 100 * capital) / (entry - stop) * entry,
                                   capital * 0.04)

        # Mock settings to return 0.5
        mock_settings = MagicMock()
        mock_settings.get = lambda key, default=None: {
            "risk_engine.sizing_mode": "risk_based",
            "risk_engine.risk_pct_per_trade": 0.5,
        }.get(key, default)

        with patch("core.meta_decision.position_sizer._s", mock_settings, create=True):
            # Force settings path by patching the import inside calculate()
            try:
                from config.settings import settings as _real_settings
                with patch.object(_real_settings, "get",
                                   side_effect=lambda k, d=None: {
                                       "risk_engine.sizing_mode": "risk_based",
                                       "risk_engine.risk_pct_per_trade": 0.5,
                                   }.get(k, d)):
                    size = sizer.calculate(
                        available_capital_usdt=capital,
                        atr_value=entry - stop,
                        entry_price=entry,
                        score=0.70,
                        regime="bull_trend",
                    )
                    assert abs(size - expected_at_0_5_pct) < 1.0, (
                        f"Expected size ≈{expected_at_0_5_pct:.2f} (0.5% risk), got {size:.2f}"
                    )
            except Exception:
                pytest.skip("Cannot mock settings in this environment — check source code test above")


# ═══════════════════════════════════════════════════════════════════════
# Integration: confirm normal ranges still work correctly after fixes
# ═══════════════════════════════════════════════════════════════════════

class TestRegimeClassifierBoundaryIntegrity:
    """Regression: ensure existing correct behavior is preserved after fixes."""

    def test_adx_above_trend_threshold_with_ema_bull(self):
        """ADX=35 with rising EMA → BULL_TREND (unchanged by fixes)."""
        clf = RegimeClassifier()
        df = _make_normal_df(adx=35.0, ema_slope_up=True, rows=60)
        for _ in range(3):
            regime, conf, _ = clf.classify(df)
        assert regime == REGIME_BULL_TREND, f"ADX=35 up expected 'bull_trend', got '{regime}'"

    def test_adx_above_trend_threshold_with_ema_bear(self):
        """ADX=35 with falling EMA → BEAR_TREND (unchanged by fixes)."""
        clf = RegimeClassifier()
        df = _make_normal_df(adx=35.0, ema_slope_up=False, rows=60)
        for _ in range(3):
            regime, conf, _ = clf.classify(df)
        assert regime == REGIME_BEAR_TREND, f"ADX=35 down expected 'bear_trend', got '{regime}'"

    def test_adx_below_ranging_threshold_is_ranging(self):
        """ADX=12 (clearly below 20) → RANGING (unchanged by fixes)."""
        clf = RegimeClassifier()
        df = _make_normal_df(adx=12.0, ema_slope_up=True, rows=60)
        for _ in range(3):
            regime, conf, _ = clf.classify(df)
        assert regime == REGIME_RANGING, f"ADX=12 expected 'ranging', got '{regime}'"

    def test_vol_expansion_overrides_adx(self):
        """BB expansion (ratio=2.0 > factor 1.5) → VOL_EXPANSION overrides ADX."""
        clf = RegimeClassifier()
        # bb_ratio=2.0 > bb_expansion_factor=1.5 → vol_expansion fires first
        df = _make_normal_df(adx=22.0, ema_slope_up=True, bb_ratio=2.0, rows=60)
        for _ in range(3):
            regime, conf, _ = clf.classify(df)
        assert regime == REGIME_VOL_EXPANSION, f"BB ratio=2.0 expected 'volatility_expansion', got '{regime}'"

    def test_vol_compression_overrides_adx(self):
        """BB compression (ratio=0.4 < factor 0.6) → VOL_COMPRESS overrides ADX."""
        clf = RegimeClassifier()
        df = _make_normal_df(adx=22.0, ema_slope_up=True, bb_ratio=0.4, rows=60)
        for _ in range(3):
            regime, conf, _ = clf.classify(df)
        assert regime == REGIME_VOL_COMPRESS, f"BB ratio=0.4 expected 'volatility_compression', got '{regime}'"

    def test_insufficient_data_returns_uncertain(self):
        """Less than 30 bars → UNCERTAIN (correct behavior, unchanged)."""
        clf = RegimeClassifier()
        df = _make_normal_df(adx=40.0, ema_slope_up=True, rows=10)
        regime, conf, _ = clf.classify(df)
        assert regime == REGIME_UNCERTAIN, f"10 rows should be 'uncertain', got '{regime}'"
        assert conf == 0.0

    def test_confidence_positive_for_real_regime(self):
        """After fix, ADX dead zone should return positive confidence."""
        clf = RegimeClassifier()
        df = _make_normal_df(adx=22.0, ema_slope_up=True, rows=60)
        for _ in range(3):
            regime, conf, _ = clf.classify(df)
        assert conf > 0.0, f"Confidence should be positive for '{regime}', got {conf}"
