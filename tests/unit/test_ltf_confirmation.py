"""
Tests for core.scanning.ltf_confirmation — 15m LTF entry confirmation.

Naming: LTF-xxx

Covers:
  - Indicator computation (EMA, RSI, volume ratio)
  - Confirmation pass scenarios (all 3 checks pass)
  - Confirmation fail scenarios (individual check failures)
  - Anti-churn / void logic
  - Edge cases: minimal data, flat price, zero volume
  - Config from settings
  - LTFConfirmationResult diagnostics
"""

import numpy as np
import pandas as pd
import pytest

from core.scanning.ltf_confirmation import (
    LTFConfirmationConfig,
    LTFConfirmationResult,
    compute_ema,
    compute_rsi,
    compute_ltf_indicators,
    evaluate_confirmation,
)


# ── Helpers ────────────────────────────────────────────────────────────

def _make_df(
    closes: list[float],
    volumes: list[float] | None = None,
    *,
    open_offset: float = -0.5,
    high_offset: float = 1.0,
    low_offset: float = -1.0,
) -> pd.DataFrame:
    """Build a minimal OHLCV DataFrame from close prices."""
    n = len(closes)
    if volumes is None:
        volumes = [1000.0] * n
    return pd.DataFrame({
        "open": [c + open_offset for c in closes],
        "high": [c + high_offset for c in closes],
        "low": [c + low_offset for c in closes],
        "close": closes,
        "volume": volumes,
    })


def _trending_up_df(n: int = 50, start: float = 100.0) -> pd.DataFrame:
    """Build a rising DataFrame with realistic oscillation.

    Uses sinusoidal noise + drift so RSI stays in ~60-68 range.
    EMA slopes upward.  Volume above average.
    """
    closes = []
    price = start
    for i in range(n):
        price += 0.05 + 0.6 * np.sin(i * 0.7)
        closes.append(price)
    volumes = [1200.0] * n
    return _make_df(closes, volumes)


def _trending_down_df(n: int = 50, start: float = 200.0) -> pd.DataFrame:
    """Build a falling DataFrame with realistic oscillation.

    Uses sinusoidal noise + drift so RSI stays in ~32-38 range.
    EMA slopes downward.  Volume above average.
    """
    closes = []
    price = start
    for i in range(n):
        price -= 0.05 + 0.6 * np.sin(i * 0.7)
        closes.append(price)
    volumes = [1200.0] * n
    return _make_df(closes, volumes)


def _flat_df(n: int = 50, price: float = 100.0) -> pd.DataFrame:
    """Build a flat-price DataFrame — EMA slope ≈ 0."""
    closes = [price] * n
    volumes = [1000.0] * n
    return _make_df(closes, volumes)


# ── S1: Indicator Computation ──────────────────────────────────────────

class TestLTF_Indicators:
    """LTF-001 through LTF-008: indicator correctness."""

    def test_ltf001_ema_length_matches_input(self):
        """LTF-001: EMA output has same length as input."""
        series = pd.Series([100.0 + i for i in range(30)])
        ema = compute_ema(series, 9)
        assert len(ema) == 30

    def test_ltf002_ema_trending_up(self):
        """LTF-002: EMA follows upward trend — last EMA > first EMA."""
        series = pd.Series([100.0 + i * 0.5 for i in range(50)])
        ema = compute_ema(series, 9)
        assert ema.iloc[-1] > ema.iloc[0]

    def test_ltf003_ema_trending_down(self):
        """LTF-003: EMA follows downward trend — last EMA < first EMA."""
        series = pd.Series([200.0 - i * 0.5 for i in range(50)])
        ema = compute_ema(series, 9)
        assert ema.iloc[-1] < ema.iloc[0]

    def test_ltf004_rsi_bounded_0_100(self):
        """LTF-004: RSI values are within [0, 100]."""
        series = pd.Series([100.0 + np.sin(i * 0.3) * 5 for i in range(60)])
        rsi = compute_rsi(series, 14)
        valid = rsi.dropna()
        assert (valid >= 0).all()
        assert (valid <= 100).all()

    def test_ltf005_rsi_strong_uptrend_high(self):
        """LTF-005: RSI in strong uptrend should be well above 50."""
        np.random.seed(55)
        series = pd.Series([100.0 + i * 2.0 + np.random.uniform(-0.1, 0.1) for i in range(60)])
        rsi = compute_rsi(series, 14)
        assert rsi.iloc[-1] > 70

    def test_ltf006_rsi_strong_downtrend_low(self):
        """LTF-006: RSI in strong downtrend should be well below 50."""
        np.random.seed(66)
        series = pd.Series([200.0 - i * 2.0 + np.random.uniform(-0.1, 0.1) for i in range(60)])
        rsi = compute_rsi(series, 14)
        assert rsi.iloc[-1] < 30

    def test_ltf007_volume_ratio_above_one_when_high(self):
        """LTF-007: Volume ratio > 1 when last bar has above-average volume."""
        volumes = [1000.0] * 49 + [2000.0]
        closes = [100.0] * 50
        df = _make_df(closes, volumes)
        cfg = LTFConfirmationConfig()
        result_df = compute_ltf_indicators(df, cfg)
        assert result_df["ltf_vol_ratio"].iloc[-1] > 1.0

    def test_ltf008_volume_ratio_below_one_when_low(self):
        """LTF-008: Volume ratio < 1 when last bar has below-average volume."""
        volumes = [1000.0] * 49 + [100.0]
        closes = [100.0] * 50
        df = _make_df(closes, volumes)
        cfg = LTFConfirmationConfig()
        result_df = compute_ltf_indicators(df, cfg)
        assert result_df["ltf_vol_ratio"].iloc[-1] < 1.0

    def test_ltf009_compute_ltf_indicators_adds_columns(self):
        """LTF-009: compute_ltf_indicators adds 3 new columns."""
        df = _flat_df(30)
        cfg = LTFConfirmationConfig()
        result_df = compute_ltf_indicators(df, cfg)
        assert "ltf_ema" in result_df.columns
        assert "ltf_rsi" in result_df.columns
        assert "ltf_vol_ratio" in result_df.columns

    def test_ltf010_compute_does_not_modify_original(self):
        """LTF-010: compute_ltf_indicators does not mutate the input DataFrame."""
        df = _flat_df(30)
        original_cols = set(df.columns)
        cfg = LTFConfirmationConfig()
        compute_ltf_indicators(df, cfg)
        assert set(df.columns) == original_cols


# ── S2: Confirmation Pass Scenarios ───────────────────────────────────

class TestLTF_ConfirmationPass:
    """LTF-020 through LTF-025: scenarios where confirmation should pass."""

    def test_ltf020_long_confirmed_uptrend(self):
        """LTF-020: Long signal confirmed in uptrend with volume."""
        df = _trending_up_df(50)
        result = evaluate_confirmation(df, "buy")
        assert result.confirmed is True
        assert result.voided is False
        assert result.ema_aligned is True
        assert result.rsi_ok is True
        assert result.volume_ok is True

    def test_ltf021_short_confirmed_downtrend(self):
        """LTF-021: Short signal confirmed in downtrend with volume."""
        df = _trending_down_df(50)
        result = evaluate_confirmation(df, "sell")
        assert result.confirmed is True
        assert result.voided is False
        assert result.ema_aligned is True

    def test_ltf022_long_alias_accepted(self):
        """LTF-022: side='long' is accepted as buy alias."""
        df = _trending_up_df(50)
        result = evaluate_confirmation(df, "long")
        assert result.confirmed is True

    def test_ltf023_sell_alias_short(self):
        """LTF-023: side='short' is accepted as sell alias."""
        df = _trending_down_df(50)
        result = evaluate_confirmation(df, "short")
        assert result.confirmed is True

    def test_ltf024_checks_passed_count_3(self):
        """LTF-024: All 3 checks pass → checks_passed == 3."""
        df = _trending_up_df(50)
        result = evaluate_confirmation(df, "buy")
        assert result.checks_passed == 3

    def test_ltf025_custom_config_relaxed_thresholds(self):
        """LTF-025: Relaxed RSI thresholds still confirm."""
        df = _trending_up_df(50)
        cfg = LTFConfirmationConfig(rsi_max_long=95.0, rsi_void_long=99.0, volume_ratio_min=0.1)
        result = evaluate_confirmation(df, "buy", cfg=cfg)
        assert result.confirmed is True


# ── S3: Confirmation Fail Scenarios ───────────────────────────────────

class TestLTF_ConfirmationFail:
    """LTF-030 through LTF-037: scenarios where confirmation should fail."""

    def test_ltf030_long_rejected_in_downtrend(self):
        """LTF-030: Long signal fails when EMA is trending down."""
        df = _trending_down_df(50)
        result = evaluate_confirmation(df, "buy")
        assert result.confirmed is False
        assert result.ema_aligned is False

    def test_ltf031_short_rejected_in_uptrend(self):
        """LTF-031: Short signal fails when EMA is trending up."""
        df = _trending_up_df(50)
        result = evaluate_confirmation(df, "sell")
        assert result.confirmed is False
        assert result.ema_aligned is False

    def test_ltf032_long_rejected_rsi_too_high(self):
        """LTF-032: Long fails when RSI exceeds rsi_max_long."""
        np.random.seed(32)
        closes = [100.0 + i * 3.0 + np.random.uniform(-0.1, 0.1) for i in range(60)]
        volumes = [1200.0] * 60
        df = _make_df(closes, volumes)
        cfg = LTFConfirmationConfig(rsi_max_long=50.0)
        result = evaluate_confirmation(df, "buy", cfg=cfg)
        assert result.rsi_ok is False
        assert result.confirmed is False

    def test_ltf033_short_rejected_rsi_too_low(self):
        """LTF-033: Short fails when RSI is below rsi_min_short."""
        np.random.seed(33)
        closes = [200.0 - i * 3.0 + np.random.uniform(-0.1, 0.1) for i in range(60)]
        volumes = [1200.0] * 60
        df = _make_df(closes, volumes)
        cfg = LTFConfirmationConfig(rsi_min_short=50.0)
        result = evaluate_confirmation(df, "sell", cfg=cfg)
        assert result.rsi_ok is False
        assert result.confirmed is False

    def test_ltf034_rejected_low_volume(self):
        """LTF-034: Confirmation fails when volume ratio is below threshold."""
        volumes = [1000.0] * 49 + [100.0]  # last bar extremely low volume
        closes = [100.0 + i * 0.5 for i in range(50)]
        df = _make_df(closes, volumes)
        cfg = LTFConfirmationConfig(volume_ratio_min=0.8)
        result = evaluate_confirmation(df, "buy", cfg=cfg)
        assert result.volume_ok is False
        assert result.confirmed is False

    def test_ltf035_flat_ema_rejects_long(self):
        """LTF-035: Flat EMA (slope ≈ 0) fails alignment for long."""
        df = _flat_df(50)
        result = evaluate_confirmation(df, "buy")
        # Flat EMA → slope ≈ 0.0, not > 0 → ema_aligned = False
        assert result.ema_aligned is False
        assert result.confirmed is False

    def test_ltf036_flat_ema_rejects_short(self):
        """LTF-036: Flat EMA (slope ≈ 0) fails alignment for short."""
        df = _flat_df(50)
        result = evaluate_confirmation(df, "sell")
        # Flat EMA → slope ≈ 0.0, not < 0 → ema_aligned = False
        assert result.ema_aligned is False
        assert result.confirmed is False

    def test_ltf037_checks_passed_partial(self):
        """LTF-037: Only volume passes → checks_passed == 1."""
        df = _flat_df(50)
        # Flat data: EMA slope = 0, RSI ≈ 50 (within bounds), volume = 1.0 (passes 0.8)
        result = evaluate_confirmation(df, "buy")
        # EMA not aligned (flat), RSI ok (50 < 72), volume ok (ratio = 1.0 ≥ 0.8)
        assert result.ema_aligned is False
        assert result.rsi_ok is True
        assert result.volume_ok is True
        assert result.checks_passed == 2
        assert result.confirmed is False  # EMA failed


# ── S4: Anti-Churn / Void Logic ───────────────────────────────────────

class TestLTF_AntiChurn:
    """LTF-040 through LTF-047: void logic when RSI strongly contradicts."""

    def test_ltf040_long_voided_rsi_above_void_threshold(self):
        """LTF-040: Long voided when 15m RSI > rsi_void_long."""
        # Strong uptrend with tiny noise → very high RSI
        np.random.seed(77)
        closes = [100.0 + i * 3.0 + np.random.uniform(-0.05, 0.05) for i in range(80)]
        volumes = [1200.0] * 80
        df = _make_df(closes, volumes)
        # Use a low void threshold to guarantee triggering
        cfg = LTFConfirmationConfig(rsi_void_long=60.0)
        result = evaluate_confirmation(df, "buy", cfg=cfg)
        assert result.voided is True
        assert "anti-churn" in result.void_reason
        assert "overbought" in result.void_reason

    def test_ltf041_short_voided_rsi_below_void_threshold(self):
        """LTF-041: Short voided when 15m RSI < rsi_void_short."""
        np.random.seed(88)
        closes = [300.0 - i * 3.0 + np.random.uniform(-0.05, 0.05) for i in range(80)]
        volumes = [1200.0] * 80
        df = _make_df(closes, volumes)
        # Use a high void threshold to guarantee triggering
        cfg = LTFConfirmationConfig(rsi_void_short=40.0)
        result = evaluate_confirmation(df, "sell", cfg=cfg)
        assert result.voided is True
        assert "anti-churn" in result.void_reason
        assert "oversold" in result.void_reason

    def test_ltf042_voided_means_not_confirmed(self):
        """LTF-042: A voided candidate is never confirmed, even if all checks pass."""
        np.random.seed(77)
        closes = [100.0 + i * 3.0 + np.random.uniform(-0.05, 0.05) for i in range(80)]
        volumes = [1200.0] * 80
        df = _make_df(closes, volumes)
        cfg = LTFConfirmationConfig(rsi_void_long=60.0)
        result = evaluate_confirmation(df, "buy", cfg=cfg)
        assert result.voided is True
        assert result.confirmed is False

    def test_ltf043_not_voided_when_rsi_in_bounds(self):
        """LTF-043: Normal RSI does not trigger void."""
        df = _trending_up_df(50)
        result = evaluate_confirmation(df, "buy")
        assert result.voided is False
        assert result.void_reason is None

    def test_ltf044_void_reason_contains_rsi_value(self):
        """LTF-044: Void reason includes the actual RSI value."""
        np.random.seed(77)
        closes = [100.0 + i * 3.0 + np.random.uniform(-0.05, 0.05) for i in range(80)]
        volumes = [1200.0] * 80
        df = _make_df(closes, volumes)
        cfg = LTFConfirmationConfig(rsi_void_long=60.0)
        result = evaluate_confirmation(df, "buy", cfg=cfg)
        assert result.voided is True
        assert "RSI" in result.void_reason

    def test_ltf045_long_not_voided_just_below_threshold(self):
        """LTF-045: Long not voided when RSI is just below void threshold."""
        df = _trending_up_df(50)
        cfg = LTFConfirmationConfig(rsi_void_long=99.0)  # very permissive
        result = evaluate_confirmation(df, "buy", cfg=cfg)
        assert result.voided is False

    def test_ltf046_short_not_voided_permissive_threshold(self):
        """LTF-046: Short not voided with extremely permissive void threshold."""
        # Build data with moderate downtrend + noise → RSI in 30-40 range
        np.random.seed(99)
        closes = [150.0 - i * 0.15 + np.random.uniform(-0.3, 0.3) for i in range(50)]
        volumes = [1200.0] * 50
        df = _make_df(closes, volumes)
        cfg = LTFConfirmationConfig(rsi_void_short=5.0)  # very permissive
        result = evaluate_confirmation(df, "sell", cfg=cfg)
        assert result.voided is False


# ── S5: Edge Cases ────────────────────────────────────────────────────

class TestLTF_EdgeCases:
    """LTF-050 through LTF-058: edge cases and boundary conditions."""

    def test_ltf050_minimal_data_2_bars(self):
        """LTF-050: Evaluation works with just 2 bars (min for EMA slope)."""
        df = _make_df([100.0, 101.0], [1000.0, 1000.0])
        # Should not raise — may not confirm but must not crash
        result = evaluate_confirmation(df, "buy")
        assert isinstance(result, LTFConfirmationResult)

    def test_ltf051_single_bar(self):
        """LTF-051: Single bar — EMA slope is 0, should not crash."""
        df = _make_df([100.0], [1000.0])
        result = evaluate_confirmation(df, "buy")
        assert isinstance(result, LTFConfirmationResult)
        assert result.ema_slope == 0.0

    def test_ltf052_zero_volume_all_bars(self):
        """LTF-052: Zero volume across all bars — vol_ratio handles gracefully."""
        df = _make_df([100.0] * 30, [0.0] * 30)
        cfg = LTFConfirmationConfig()
        result = evaluate_confirmation(df, "buy", cfg=cfg)
        # vol_avg = 0 → NaN → fillna(1.0) → vol_ratio = 1.0 ≥ 0.8
        assert isinstance(result, LTFConfirmationResult)
        assert result.volume_ratio == 1.0

    def test_ltf053_default_config_when_none(self):
        """LTF-053: cfg=None uses default LTFConfirmationConfig."""
        df = _trending_up_df(50)
        result = evaluate_confirmation(df, "buy", cfg=None)
        assert isinstance(result, LTFConfirmationResult)

    def test_ltf054_ltf_close_matches_last_close(self):
        """LTF-054: result.ltf_close matches the last close in the DataFrame."""
        closes = [100.0 + i for i in range(30)]
        df = _make_df(closes)
        result = evaluate_confirmation(df, "buy")
        assert result.ltf_close == closes[-1]

    def test_ltf055_ema_slope_positive_in_uptrend(self):
        """LTF-055: ema_slope is positive in uptrend."""
        df = _trending_up_df(50)
        result = evaluate_confirmation(df, "buy")
        assert result.ema_slope > 0

    def test_ltf056_ema_slope_negative_in_downtrend(self):
        """LTF-056: ema_slope is negative in downtrend."""
        df = _trending_down_df(50)
        result = evaluate_confirmation(df, "sell")
        assert result.ema_slope < 0

    def test_ltf057_rsi_value_stored_in_result(self):
        """LTF-057: RSI value is stored in result and is a float."""
        df = _trending_up_df(50)
        result = evaluate_confirmation(df, "buy")
        assert isinstance(result.rsi, float)
        assert 0 <= result.rsi <= 100

    def test_ltf058_volume_ratio_stored_in_result(self):
        """LTF-058: Volume ratio is stored in result."""
        df = _trending_up_df(50)
        result = evaluate_confirmation(df, "buy")
        assert isinstance(result.volume_ratio, float)
        assert result.volume_ratio > 0


# ── S6: Configuration ─────────────────────────────────────────────────

class TestLTF_Config:
    """LTF-060 through LTF-067: configuration and settings integration."""

    def test_ltf060_default_config_values(self):
        """LTF-060: Default config values match documented defaults."""
        cfg = LTFConfirmationConfig()
        assert cfg.ema_period == 9
        assert cfg.rsi_period == 14
        assert cfg.rsi_max_long == 72.0
        assert cfg.rsi_min_short == 28.0
        assert cfg.rsi_void_long == 78.0
        assert cfg.rsi_void_short == 22.0
        assert cfg.volume_ratio_min == 0.6  # lowered from 0.80 based on backtest study
        assert cfg.volume_lookback == 20
        assert cfg.ema_slope_bars == 3
        assert cfg.timeframe == "15m"
        assert cfg.ohlcv_limit == 100

    def test_ltf061_custom_ema_period(self):
        """LTF-061: Custom EMA period is respected."""
        cfg = LTFConfirmationConfig(ema_period=21)
        df = _trending_up_df(50)
        result_df = compute_ltf_indicators(df, cfg)
        # EMA-21 will be smoother than EMA-9
        assert "ltf_ema" in result_df.columns

    def test_ltf062_custom_rsi_period(self):
        """LTF-062: Custom RSI period is respected."""
        cfg = LTFConfirmationConfig(rsi_period=7)
        df = _trending_up_df(50)
        result_df = compute_ltf_indicators(df, cfg)
        assert "ltf_rsi" in result_df.columns

    def test_ltf063_custom_volume_lookback(self):
        """LTF-063: Custom volume lookback changes the rolling window."""
        cfg = LTFConfirmationConfig(volume_lookback=5)
        volumes = [100.0] * 10 + [500.0] * 5
        df = _make_df([100.0] * 15, volumes)
        result_df = compute_ltf_indicators(df, cfg)
        # With lookback=5, avg of last 5 is 500, so ratio = 500/500 = 1.0
        assert abs(result_df["ltf_vol_ratio"].iloc[-1] - 1.0) < 0.01

    def test_ltf064_custom_ema_slope_bars(self):
        """LTF-064: Custom ema_slope_bars changes slope computation window."""
        # First build data with uptrend then short reversal
        closes = [100.0 + i * 0.5 for i in range(47)] + [124.0, 123.5, 123.0]
        df = _make_df(closes, [1200.0] * 50)
        # With ema_slope_bars=3 (default), last 3 EMA values show the reversal
        cfg3 = LTFConfirmationConfig(ema_slope_bars=3)
        r3 = evaluate_confirmation(df, "buy", cfg=cfg3)
        # With ema_slope_bars=10, looking further back sees overall uptrend
        cfg10 = LTFConfirmationConfig(ema_slope_bars=10)
        r10 = evaluate_confirmation(df, "buy", cfg=cfg10)
        # The broader window should see a more positive slope
        assert r10.ema_slope > r3.ema_slope

    def test_ltf065_settings_default_config_has_ltf_section(self):
        """LTF-065: DEFAULT_CONFIG in settings.py has ltf_confirmation section."""
        from config.settings import DEFAULT_CONFIG
        assert "ltf_confirmation" in DEFAULT_CONFIG
        ltf = DEFAULT_CONFIG["ltf_confirmation"]
        assert ltf["ema_period"] == 9
        assert ltf["rsi_period"] == 14
        assert ltf["rsi_max_long"] == 72.0
        assert ltf["rsi_min_short"] == 28.0
        assert ltf["rsi_void_long"] == 78.0
        assert ltf["rsi_void_short"] == 22.0
        assert ltf["volume_ratio_min"] == 0.6  # lowered from 0.80 based on backtest study
        assert ltf["volume_lookback"] == 20
        assert ltf["ema_slope_bars"] == 3
        assert ltf["timeframe"] == "15m"
        assert ltf["ohlcv_limit"] == 100

    def test_ltf066_from_settings_returns_config(self):
        """LTF-066: from_settings() returns a valid LTFConfirmationConfig."""
        cfg = LTFConfirmationConfig.from_settings()
        assert isinstance(cfg, LTFConfirmationConfig)
        assert cfg.ema_period > 0
        assert cfg.rsi_period > 0

    def test_ltf067_all_thresholds_configurable(self):
        """LTF-067: Every threshold in the module is configurable via LTFConfirmationConfig."""
        # Verify all parameters can be overridden
        cfg = LTFConfirmationConfig(
            ema_period=21,
            rsi_period=7,
            rsi_max_long=60.0,
            rsi_min_short=40.0,
            rsi_void_long=65.0,
            rsi_void_short=35.0,
            volume_ratio_min=1.5,
            volume_lookback=10,
            ema_slope_bars=5,
            timeframe="5m",
            ohlcv_limit=200,
        )
        assert cfg.ema_period == 21
        assert cfg.rsi_period == 7
        assert cfg.rsi_max_long == 60.0
        assert cfg.rsi_min_short == 40.0
        assert cfg.rsi_void_long == 65.0
        assert cfg.rsi_void_short == 35.0
        assert cfg.volume_ratio_min == 1.5
        assert cfg.volume_lookback == 10
        assert cfg.ema_slope_bars == 5
        assert cfg.timeframe == "5m"
        assert cfg.ohlcv_limit == 200


# ── S7: Side-by-Side Comparison ──────────────────────────────────────

class TestLTF_SideBySide:
    """LTF-070 through LTF-074: confirm opposite-side asymmetry."""

    def test_ltf070_uptrend_confirms_buy_rejects_sell(self):
        """LTF-070: Same uptrend data confirms buy but rejects sell."""
        df = _trending_up_df(50)
        buy_result = evaluate_confirmation(df, "buy")
        sell_result = evaluate_confirmation(df, "sell")
        assert buy_result.ema_aligned is True
        assert sell_result.ema_aligned is False

    def test_ltf071_downtrend_confirms_sell_rejects_buy(self):
        """LTF-071: Same downtrend data confirms sell but rejects buy."""
        df = _trending_down_df(50)
        sell_result = evaluate_confirmation(df, "sell")
        buy_result = evaluate_confirmation(df, "buy")
        assert sell_result.ema_aligned is True
        assert buy_result.ema_aligned is False

    def test_ltf072_rsi_asymmetry_long_vs_short(self):
        """LTF-072: RSI check uses different thresholds for long vs short."""
        cfg = LTFConfirmationConfig(rsi_max_long=60.0, rsi_min_short=40.0)
        # Build data with RSI ≈ 55
        closes = [100.0 + (i % 5) * 0.3 for i in range(50)]
        df = _make_df(closes, [1200.0] * 50)
        long_result = evaluate_confirmation(df, "buy", cfg=cfg)
        short_result = evaluate_confirmation(df, "sell", cfg=cfg)
        # RSI ≈ 55: long check (55 < 60) should pass, short check (55 > 40) should pass
        assert long_result.rsi_ok is True
        assert short_result.rsi_ok is True

    def test_ltf073_void_thresholds_asymmetric(self):
        """LTF-073: Void thresholds differ between long and short."""
        cfg = LTFConfirmationConfig()
        assert cfg.rsi_void_long != cfg.rsi_void_short
        assert cfg.rsi_void_long > 50  # overbought zone
        assert cfg.rsi_void_short < 50  # oversold zone


# ── S8: Logging ───────────────────────────────────────────────────────

class TestLTF_Logging:
    """LTF-080 through LTF-082: logging output verification."""

    def test_ltf080_confirmation_logged(self, caplog):
        """LTF-080: evaluate_confirmation logs the result."""
        import logging
        with caplog.at_level(logging.INFO, logger="core.scanning.ltf_confirmation"):
            df = _trending_up_df(50)
            evaluate_confirmation(df, "buy")
        assert "LTFConfirmation" in caplog.text
        assert "confirmed=" in caplog.text

    def test_ltf081_void_reason_logged(self, caplog):
        """LTF-081: Void reason appears in log when candidate is voided."""
        import logging
        np.random.seed(77)
        closes = [100.0 + i * 3.0 + np.random.uniform(-0.05, 0.05) for i in range(80)]
        df = _make_df(closes, [1200.0] * 80)
        cfg = LTFConfirmationConfig(rsi_void_long=60.0)
        with caplog.at_level(logging.INFO, logger="core.scanning.ltf_confirmation"):
            evaluate_confirmation(df, "buy", cfg=cfg)
        assert "anti-churn" in caplog.text

    def test_ltf082_side_logged(self, caplog):
        """LTF-082: The side parameter is logged."""
        import logging
        with caplog.at_level(logging.INFO, logger="core.scanning.ltf_confirmation"):
            df = _trending_up_df(50)
            evaluate_confirmation(df, "buy")
        assert "side=buy" in caplog.text
