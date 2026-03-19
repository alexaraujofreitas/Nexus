"""
tests/unit/test_trend_model.py
-------------------------------
Regression tests for TrendModel signal generation, with emphasis on the
"uncertain" regime branch that was missing and caused 0 IDSS candidates
during the most common market condition (71% of runtime).

  TM-01  Uncertain regime: long signal fires when EMA9 > EMA21, ADX > 25, RSI in 45–70
  TM-02  Uncertain regime: short signal fires when EMA9 < EMA21, ADX > 25, RSI in 30–55
  TM-03  Uncertain regime: returns None when EMA9 == EMA21 (no clear direction)
  TM-04  Uncertain regime: returns None when ADX < 25 (no confirmed momentum)
  TM-05  Uncertain regime: RSI out of zone returns None
  TM-06  Unknown / unrecognised regime returns None (no silent pass-through)
  TM-07  Bull trend long signal still fires correctly (regression guard)
  TM-08  Bear trend short signal still fires correctly (regression guard)
  TM-09  Uncertain long: strength is slightly lower than equivalent bull_trend signal
  TM-10  DataFrame with < 50 rows returns None regardless of regime
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from core.signals.sub_models.trend_model import TrendModel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_df(
    rows: int = 100,
    close: float = 50_000.0,
    ema9: float = 50_200.0,
    ema21: float = 49_800.0,
    ema20: float = 50_100.0,
    ema100: float = 48_000.0,
    adx: float = 30.0,
    rsi: float = 55.0,
    macd: float = 100.0,
    macd_signal: float = 80.0,
) -> pd.DataFrame:
    """
    Build a minimal DataFrame with the indicator columns TrendModel.evaluate() reads.
    All values are constant across rows for simplicity; only the last row matters.
    """
    data = {
        "open":        [close] * rows,
        "high":        [close * 1.005] * rows,
        "low":         [close * 0.995] * rows,
        "close":       [close] * rows,
        "volume":      [1_000.0] * rows,
        "ema_9":       [ema9] * rows,
        "ema_21":      [ema21] * rows,
        "ema_20":      [ema20] * rows,
        "ema_100":     [ema100] * rows,
        "adx":         [adx] * rows,
        "rsi_14":      [rsi] * rows,
        "macd":        [macd] * rows,
        "macd_signal": [macd_signal] * rows,
        # ATR approximation (needed by _atr())
        "atr_14":      [close * 0.01] * rows,
    }
    return pd.DataFrame(data)


SYMBOL = "BTC/USDT"
TIMEFRAME = "1h"
MODEL = TrendModel()


# ---------------------------------------------------------------------------
# TM-01 — Uncertain regime: long signal fires
# ---------------------------------------------------------------------------

class TestUncertainRegimeLong:
    def test_tm01_returns_model_signal(self):
        """Long signal must be returned for uncertain regime with bullish indicators."""
        df = _make_df(ema9=50_200.0, ema21=49_800.0, adx=30.0, rsi=55.0)
        result = MODEL.evaluate(SYMBOL, df, regime="uncertain", timeframe=TIMEFRAME)
        assert result is not None, (
            "TrendModel must return a signal for uncertain regime when EMA9 > EMA21 "
            "and RSI is in the 45–70 zone. Got None — this was the root cause of "
            "0 IDSS candidates during overnight trading."
        )

    def test_tm01_direction_is_long(self):
        df = _make_df(ema9=50_200.0, ema21=49_800.0, adx=30.0, rsi=55.0)
        result = MODEL.evaluate(SYMBOL, df, regime="uncertain", timeframe=TIMEFRAME)
        assert result.direction == "long"

    def test_tm01_strength_is_positive(self):
        df = _make_df(ema9=50_200.0, ema21=49_800.0, adx=30.0, rsi=55.0)
        result = MODEL.evaluate(SYMBOL, df, regime="uncertain", timeframe=TIMEFRAME)
        assert result.strength > 0.0

    def test_tm01_signal_has_valid_sl_tp(self):
        df = _make_df(ema9=50_200.0, ema21=49_800.0, adx=30.0, rsi=55.0)
        result = MODEL.evaluate(SYMBOL, df, regime="uncertain", timeframe=TIMEFRAME)
        assert result.stop_loss > 0.0
        assert result.take_profit > result.entry_price, (
            "Take-profit must be above entry for a long signal"
        )
        assert result.stop_loss < result.entry_price, (
            "Stop-loss must be below entry for a long signal"
        )

    def test_tm01_rationale_mentions_uncertain(self):
        df = _make_df(ema9=50_200.0, ema21=49_800.0, adx=30.0, rsi=55.0)
        result = MODEL.evaluate(SYMBOL, df, regime="uncertain", timeframe=TIMEFRAME)
        assert "uncertain" in result.rationale.lower(), (
            "Rationale must identify the uncertain regime for transparency"
        )


# ---------------------------------------------------------------------------
# TM-02 — Uncertain regime: short signal fires
# ---------------------------------------------------------------------------

class TestUncertainRegimeShort:
    def test_tm02_returns_model_signal(self):
        """Short signal must fire for uncertain regime with bearish indicators."""
        df = _make_df(
            ema9=49_800.0, ema21=50_200.0,   # ema9 < ema21
            adx=30.0,
            rsi=42.0,                          # in bear zone 30–55
            macd=-100.0, macd_signal=-50.0,   # macd < macd_signal
        )
        result = MODEL.evaluate(SYMBOL, df, regime="uncertain", timeframe=TIMEFRAME)
        assert result is not None, (
            "TrendModel must return a short signal for uncertain regime when "
            "EMA9 < EMA21 and RSI is in the 30–55 zone."
        )

    def test_tm02_direction_is_short(self):
        df = _make_df(
            ema9=49_800.0, ema21=50_200.0,
            adx=30.0, rsi=42.0,
            macd=-100.0, macd_signal=-50.0,
        )
        result = MODEL.evaluate(SYMBOL, df, regime="uncertain", timeframe=TIMEFRAME)
        assert result.direction == "short"

    def test_tm02_tp_below_entry_for_short(self):
        df = _make_df(
            ema9=49_800.0, ema21=50_200.0,
            adx=30.0, rsi=42.0,
            macd=-100.0, macd_signal=-50.0,
        )
        result = MODEL.evaluate(SYMBOL, df, regime="uncertain", timeframe=TIMEFRAME)
        assert result.take_profit < result.entry_price
        assert result.stop_loss > result.entry_price


# ---------------------------------------------------------------------------
# TM-03 — Uncertain regime: conflicting or flat EMAs return None
# ---------------------------------------------------------------------------

class TestUncertainRegimeNoSignal:
    def test_tm03_returns_none_when_ema9_equals_ema21(self):
        """When EMA9 == EMA21 there is no directional bias — must return None."""
        df = _make_df(ema9=50_000.0, ema21=50_000.0, adx=30.0, rsi=50.0)
        result = MODEL.evaluate(SYMBOL, df, regime="uncertain", timeframe=TIMEFRAME)
        assert result is None

    def test_tm03_returns_none_when_ema9_above_but_rsi_out_of_bull_zone(self):
        """EMA9 > EMA21 but RSI=75 (overbought, outside 45–70) → None."""
        df = _make_df(ema9=50_200.0, ema21=49_800.0, adx=30.0, rsi=75.0)
        result = MODEL.evaluate(SYMBOL, df, regime="uncertain", timeframe=TIMEFRAME)
        assert result is None

    def test_tm03_returns_none_when_ema9_below_but_rsi_out_of_bear_zone(self):
        """EMA9 < EMA21 but RSI=65 (not in bear zone 30–55) → None."""
        df = _make_df(ema9=49_800.0, ema21=50_200.0, adx=30.0, rsi=65.0)
        result = MODEL.evaluate(SYMBOL, df, regime="uncertain", timeframe=TIMEFRAME)
        assert result is None


# ---------------------------------------------------------------------------
# TM-04 — Uncertain regime: ADX < 25 always returns None
# ---------------------------------------------------------------------------

class TestUncertainRegimeADXGate:
    def test_tm04_returns_none_when_adx_below_25(self):
        """ADX < 25 means no confirmed momentum — signal must be suppressed."""
        df = _make_df(ema9=50_200.0, ema21=49_800.0, adx=20.0, rsi=55.0)
        result = MODEL.evaluate(SYMBOL, df, regime="uncertain", timeframe=TIMEFRAME)
        assert result is None, (
            "TrendModel must not fire in uncertain regime when ADX < 25 — "
            "ADX gate prevents low-conviction momentum trades"
        )

    def test_tm04_fires_when_adx_exactly_25(self):
        """ADX == 25 is the threshold — must fire at exactly 25."""
        df = _make_df(ema9=50_200.0, ema21=49_800.0, adx=25.0, rsi=55.0)
        result = MODEL.evaluate(SYMBOL, df, regime="uncertain", timeframe=TIMEFRAME)
        assert result is not None, "ADX=25 is right at the threshold — should fire"


# ---------------------------------------------------------------------------
# TM-05 — Unknown regime returns None (no silent pass-through)
# ---------------------------------------------------------------------------

class TestUnknownRegime:
    def test_tm06_unknown_regime_string_returns_none(self):
        """Any unrecognised regime string must return None — no accidental signals."""
        df = _make_df(ema9=50_200.0, ema21=49_800.0, adx=30.0, rsi=55.0)
        for bad_regime in ("sideways", "accumulation", "distribution", "", "BULL"):
            result = MODEL.evaluate(SYMBOL, df, regime=bad_regime, timeframe=TIMEFRAME)
            assert result is None, (
                f"regime='{bad_regime}' is not handled — should return None, got {result}"
            )


# ---------------------------------------------------------------------------
# TM-07 & TM-08 — Bull/bear regression guards
# ---------------------------------------------------------------------------

class TestBullBearRegimeRegression:
    def test_tm07_bull_trend_long_signal_fires(self):
        """Bull trend: long signal must still fire after uncertain branch was added."""
        from core.regime.regime_classifier import REGIME_BULL_TREND
        df = _make_df(ema9=50_200.0, ema21=49_800.0, adx=30.0, rsi=55.0)
        result = MODEL.evaluate(SYMBOL, df, regime=REGIME_BULL_TREND, timeframe=TIMEFRAME)
        assert result is not None
        assert result.direction == "long"

    def test_tm08_bear_trend_short_signal_fires(self):
        """Bear trend: short signal must still fire after uncertain branch was added."""
        from core.regime.regime_classifier import REGIME_BEAR_TREND
        df = _make_df(
            ema9=49_800.0, ema21=50_200.0,
            adx=30.0, rsi=42.0,
            macd=-100.0, macd_signal=-50.0,
        )
        result = MODEL.evaluate(SYMBOL, df, regime=REGIME_BEAR_TREND, timeframe=TIMEFRAME)
        assert result is not None
        assert result.direction == "short"


# ---------------------------------------------------------------------------
# TM-09 — Uncertain signal strength is lower than equivalent trending signal
# ---------------------------------------------------------------------------

class TestUncertainStrengthPenalty:
    def test_tm09_uncertain_strength_lower_than_bull_trend(self):
        """
        Uncertain regime uses a lower base strength (+0.10 vs +0.15 in trending).
        With identical indicators, uncertain should produce lower strength.
        """
        from core.regime.regime_classifier import REGIME_BULL_TREND
        df = _make_df(ema9=50_200.0, ema21=49_800.0, adx=30.0, rsi=55.0)
        bull_result      = MODEL.evaluate(SYMBOL, df, regime=REGIME_BULL_TREND, timeframe=TIMEFRAME)
        uncertain_result = MODEL.evaluate(SYMBOL, df, regime="uncertain",       timeframe=TIMEFRAME)
        assert bull_result is not None
        assert uncertain_result is not None
        assert uncertain_result.strength <= bull_result.strength, (
            "Uncertain regime signals should have equal or lower strength than "
            "equivalent bull_trend signals (0.10 base vs 0.15 base)"
        )


# ---------------------------------------------------------------------------
# TM-10 — Short DataFrame returns None
# ---------------------------------------------------------------------------

class TestMinimumDataGuard:
    def test_tm10_short_df_returns_none(self):
        """DataFrame with < 50 rows must return None regardless of regime."""
        df = _make_df(rows=49, ema9=50_200.0, ema21=49_800.0, adx=30.0, rsi=55.0)
        for regime in ("uncertain", "bull_trend", "bear_trend"):
            result = MODEL.evaluate(SYMBOL, df, regime=regime, timeframe=TIMEFRAME)
            assert result is None, (
                f"regime='{regime}' with 49 rows should return None — insufficient data"
            )
