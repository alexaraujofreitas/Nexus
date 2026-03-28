"""
tests/unit/test_indicator_contracts.py — Indicator Feature Contracts (IND-001 to IND-010)

Guards the indicator scan/backtest split introduced in Wave 2.
Verifies that calculate_scan_mode() satisfies the exact column requirements
of every active live-scan consumer.

DESIGN INTENT:
  These tests run on every commit.  A red test here means a live-scan consumer
  will get a KeyError at runtime.  They must pass before any scanner.py change
  ships.  They also act as the specification for what SCAN_CORE_COLUMNS contains.

CONSUMERS AND THEIR REQUIREMENTS:
  TrendModel             : ema_9, ema_20, ema_21, ema_100, adx_14/adx,
                           rsi_14/rsi, macd, macd_signal, atr_14/atr
  MomentumBreakoutModel  : rsi_14/rsi, atr_14/atr  (+ raw OHLCV)
  FundingRateModel       : atr_14/atr (+ close)
  SentimentModel         : atr_14/atr (+ close)
  OrderBookModel         : atr_14/atr — hard-gated at 1h (returns None regardless)
  RegimeClassifier rule  : adx/adx_14, ema_20, bb_upper/lower/mid/width, rsi
  RegimeClassifier HMM   : adx (features 3); others from OHLCV at runtime
  Volatility pre-filter  : atr (fallback ok but column preferred)

  All other columns (17 SMA, 13 spare EMA, SuperTrend, Ichimoku, Keltner,
  Donchian, Fibonacci, Pivot, etc.) are NOT required by any active live consumer.
"""
from __future__ import annotations

import time

import numpy as np
import pandas as pd
import pytest

from core.features.indicator_library import (
    SCAN_CORE_COLUMNS,
    calculate_all,
    calculate_scan_mode,
)


# ── shared fixture: 300-bar synthetic OHLCV ───────────────────────────────────

@pytest.fixture(scope="module")
def ohlcv_300() -> pd.DataFrame:
    """
    Synthetic 300-bar OHLCV DataFrame with a UTC DatetimeIndex.
    Prices are realistic BTC-like (50k–90k range) so ATR, BB, and ADX
    produce meaningful (non-degenerate) values.
    """
    rng = np.random.default_rng(seed=42)
    n   = 300
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")

    # Random walk log-returns → close prices in [50_000, 90_000]
    returns = rng.normal(0.0, 0.005, size=n)
    close   = 70_000.0 * np.exp(np.cumsum(returns))
    close   = np.clip(close, 45_000.0, 100_000.0)

    spread  = rng.uniform(0.003, 0.010, size=n)
    high    = close * (1.0 + spread / 2)
    low     = close * (1.0 - spread / 2)
    open_   = close * (1.0 + rng.normal(0, 0.002, n))
    volume  = rng.uniform(100, 2000, size=n)

    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


# ── IND-001 — SCAN_CORE_COLUMNS constant is populated ────────────────────────

@pytest.mark.unit
def test_scan_core_columns_constant_not_empty():
    """SCAN_CORE_COLUMNS must define at least the minimum required columns."""
    required_minimum = {
        "ema_9", "ema_20", "ema_21", "ema_100",
        "adx_14", "adx",
        "rsi_14", "rsi",
        "macd", "macd_signal",
        "atr_14", "atr",
        "bb_upper", "bb_lower", "bb_mid", "bb_width",
    }
    missing = required_minimum - SCAN_CORE_COLUMNS
    assert not missing, f"SCAN_CORE_COLUMNS is missing: {missing}"


# ── IND-002 — calculate_scan_mode produces all SCAN_CORE_COLUMNS ─────────────

@pytest.mark.unit
def test_scan_mode_all_core_columns_present(ohlcv_300):
    """Every column in SCAN_CORE_COLUMNS must be present in scan_mode output."""
    df_out = calculate_scan_mode(ohlcv_300)
    missing = [col for col in SCAN_CORE_COLUMNS if col not in df_out.columns]
    assert not missing, f"calculate_scan_mode() missing columns: {missing}"


# ── IND-003 — TrendModel column contract ─────────────────────────────────────

@pytest.mark.unit
def test_trend_model_columns_present(ohlcv_300):
    """
    TrendModel requires: ema_9, ema_20, ema_21, ema_100, adx_14 (or adx),
    rsi_14 (or rsi), macd, macd_signal, atr_14 (or atr).
    """
    df_out = calculate_scan_mode(ohlcv_300)
    required = {
        "ema_9", "ema_20", "ema_21", "ema_100",
        "adx_14", "adx",
        "rsi_14", "rsi",
        "macd", "macd_signal",
        "atr_14", "atr",
    }
    missing = required - set(df_out.columns)
    assert not missing, f"TrendModel contract failed — missing: {missing}"


# ── IND-004 — RegimeClassifier (rule-based) column contract ──────────────────

@pytest.mark.unit
def test_regime_rule_based_columns_present(ohlcv_300):
    """
    RegimeClassifier rule-based path uses: adx/adx_14, ema_20, bb_upper,
    bb_lower, bb_mid, rsi.  All must be present in scan_mode output.
    """
    df_out = calculate_scan_mode(ohlcv_300)
    required = {"adx", "ema_20", "bb_upper", "bb_lower", "bb_mid", "rsi"}
    missing = required - set(df_out.columns)
    assert not missing, f"RegimeClassifier rule-based contract failed — missing: {missing}"


# ── IND-005 — RegimeClassifier (HMM) column contract ─────────────────────────

@pytest.mark.unit
def test_regime_hmm_columns_present(ohlcv_300):
    """
    HMM feature 3 uses 'adx'.  Features 1, 2, 4 are computed from OHLCV inline.
    At minimum, 'adx' must be present in scan_mode output.
    """
    df_out = calculate_scan_mode(ohlcv_300)
    assert "adx" in df_out.columns, "HMM feature 3 requires 'adx' column"


# ── IND-006 — Volatility pre-filter column contract ──────────────────────────

@pytest.mark.unit
def test_volatility_filter_columns_present(ohlcv_300):
    """check_volatility() prefers 'atr' column (falls back if absent but column preferred)."""
    df_out = calculate_scan_mode(ohlcv_300)
    assert "atr" in df_out.columns, "Volatility filter requires 'atr' column"


# ── IND-007 — calculate_all() still intact ───────────────────────────────────

@pytest.mark.unit
def test_calculate_all_still_produces_full_set(ohlcv_300):
    """
    calculate_all() must remain completely unchanged.
    BacktestEngine, IDSSBacktester, and validation paths depend on it.
    Spot-check: must contain columns NOT in SCAN_CORE_COLUMNS.
    """
    df_out = calculate_all(ohlcv_300)
    # SuperTrend and SMA are REMOVE from scan mode but must remain in calculate_all
    assert "supertrend_10" in df_out.columns,  "calculate_all: supertrend_10 missing"
    assert "sma_20"        in df_out.columns,  "calculate_all: sma_20 missing"
    assert "ema_200"       in df_out.columns,  "calculate_all: ema_200 missing"
    assert "ichimoku_a"    not in df_out.columns or "ichi_a" in df_out.columns, \
        "calculate_all: Ichimoku columns missing"


# ── IND-008 — scan_mode columns are NOT the full set ─────────────────────────

@pytest.mark.unit
def test_scan_mode_omits_dead_weight_columns(ohlcv_300):
    """
    calculate_scan_mode() must NOT produce REMOVE-class columns.
    These are the columns confirmed unused by any active live-scan consumer.
    """
    df_out = calculate_scan_mode(ohlcv_300)
    remove_sample = {
        "sma_20", "sma_50", "wma_20",
        # NOTE: ema_50 intentionally removed from dead-weight set (Session 37).
        # PullbackLongModel requires ema_50 in the 4h HTF context df.
        # It is now declared in SCAN_CORE_COLUMNS and computed by calculate_scan_mode().
        "ema_200", "ema_55",
        "adx_20",
        "macd_hist",
        "supertrend_10", "supertrend_dir_10",
        "stoch_rsi_k", "stoch_rsi_d",
        "rsi_2", "rsi_3",
        "atr_2", "atr_3",
        "bb_pct",
        "kc_upper",
        "dc_upper",
        "obv",
        "pivot",
        "fib_382",
    }
    present = remove_sample & set(df_out.columns)
    assert not present, (
        f"calculate_scan_mode() contains dead-weight columns that should be removed: {present}"
    )


# ── IND-009 — scan_mode is non-null on core columns (last 50 bars) ───────────

@pytest.mark.unit
def test_scan_mode_core_columns_non_null_tail(ohlcv_300):
    """
    For a 300-bar input, the last 50 bars of all SCAN_CORE_COLUMNS must be
    non-null.  ATR (period 14), RSI (period 14), EMA (100) all have enough
    history by bar 100.  Null values would cause model failures at scan time.
    """
    df_out = calculate_scan_mode(ohlcv_300)
    tail = df_out.iloc[-50:]
    for col in SCAN_CORE_COLUMNS:
        if col in tail.columns:
            null_count = tail[col].isna().sum()
            assert null_count == 0, (
                f"Column '{col}' has {null_count} null values in the last 50 bars"
            )


# ── IND-010 — Performance benchmark ──────────────────────────────────────────

@pytest.mark.unit
def test_scan_mode_faster_than_calculate_all(ohlcv_300):
    """
    calculate_scan_mode() must be at least 40 % faster than calculate_all()
    on 300-bar OHLCV.  This validates the computational efficiency goal.

    Timing methodology: 3 warm-up calls followed by the measured call
    to reduce JIT/import noise.
    """
    # Warm-up (imports, JIT, etc.)
    for _ in range(3):
        calculate_scan_mode(ohlcv_300)
        calculate_all(ohlcv_300)

    # Measured runs (average of 5)
    runs = 5
    t_scan = 0.0
    t_all  = 0.0
    for _ in range(runs):
        t0 = time.perf_counter(); calculate_scan_mode(ohlcv_300); t_scan += time.perf_counter() - t0
        t0 = time.perf_counter(); calculate_all(ohlcv_300);       t_all  += time.perf_counter() - t0

    t_scan /= runs
    t_all  /= runs
    speedup = (t_all - t_scan) / t_all * 100.0

    print(
        f"\nIND-010 timing: scan_mode={t_scan*1000:.1f}ms  "
        f"calculate_all={t_all*1000:.1f}ms  "
        f"speedup={speedup:.1f}%"
    )

    assert speedup >= 40.0, (
        f"calculate_scan_mode() speedup {speedup:.1f}% < required 40%. "
        f"scan={t_scan*1000:.1f}ms  all={t_all*1000:.1f}ms"
    )
