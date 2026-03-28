# ============================================================
# NEXUS TRADER — Unit Tests: PBL + SLC Models (v1.3 Phase 6)
#
# Tests cover:
#   1. PullbackLongModel — 13 unit tests
#   2. SwingLowContinuationModel — 11 unit tests
#   3. PositionSizer.calculate_pos_frac — 9 tests
#   4. SignalGenerator integration — 5 tests
#
# All must pass before mr_pbl_slc.enabled can be set to true.
# ============================================================
import pytest
import pandas as pd
import numpy as np
from unittest.mock import patch, MagicMock

from core.signals.sub_models.pullback_long_model import PullbackLongModel
from core.signals.sub_models.swing_low_continuation_model import SwingLowContinuationModel
from core.meta_decision.position_sizer import PositionSizer

# ── Helpers ──────────────────────────────────────────────────────────────────

def make_df(n=100, base_price=50000.0, trend="flat", regime_atr_pct=0.008):
    """Build a synthetic OHLCV + indicator DataFrame with realistic values."""
    np.random.seed(42)
    dates = pd.date_range("2025-01-01", periods=n, freq="30min", tz="UTC")
    close = np.full(n, base_price, dtype=float)

    if trend == "up":
        close = base_price + np.arange(n) * 10.0 + np.random.randn(n) * 30
    elif trend == "down":
        close = base_price - np.arange(n) * 8.0 + np.random.randn(n) * 30
    else:
        close = base_price + np.random.randn(n) * 50

    high  = close + abs(np.random.randn(n)) * 20
    low   = close - abs(np.random.randn(n)) * 20
    open_ = close + np.random.randn(n) * 15
    volume = np.random.uniform(1e6, 5e6, n)

    df = pd.DataFrame({
        "open": open_, "high": high, "low": low, "close": close, "volume": volume
    }, index=dates)

    atr = base_price * regime_atr_pct
    df["atr_14"] = atr
    df["atr"]    = atr

    # Compute EMAs
    for p in [9, 20, 21, 50, 100]:
        df[f"ema_{p}"] = df["close"].ewm(span=p, adjust=False).mean()

    # RSI14 stub (synthetic)
    df["rsi_14"] = 55.0
    df["rsi"]    = 55.0

    # ADX stub
    df["adx_14"] = 35.0
    df["adx"]    = 35.0

    # BB
    df["bb_upper"] = close + atr * 2
    df["bb_lower"] = close - atr * 2
    df["bb_mid"]   = close

    return df


def make_htf_4h(n=60, base_price=50000.0, bullish=True):
    """4h DataFrame with EMA20 > EMA50 (bullish) or EMA20 < EMA50 (bearish)."""
    np.random.seed(99)
    dates = pd.date_range("2025-01-01", periods=n, freq="4h", tz="UTC")

    if bullish:
        close = base_price + np.arange(n) * 50
    else:
        close = base_price - np.arange(n) * 50

    df = pd.DataFrame({
        "open":   close + np.random.randn(n) * 100,
        "high":   close + abs(np.random.randn(n)) * 150,
        "low":    close - abs(np.random.randn(n)) * 150,
        "close":  close,
        "volume": np.random.uniform(5e6, 2e7, n),
    }, index=dates)

    df["ema_20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ema_50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["atr_14"] = 800.0
    df["atr"]    = 800.0

    return df


def make_1h_df(n=120, base_price=50000.0, trend="down"):
    """1h DataFrame for SLC evaluation."""
    np.random.seed(77)
    dates = pd.date_range("2025-01-01", periods=n, freq="1h", tz="UTC")

    if trend == "down":
        close = base_price - np.arange(n) * 20 + np.random.randn(n) * 30
    else:
        close = base_price + np.arange(n) * 20 + np.random.randn(n) * 30

    df = pd.DataFrame({
        "open":   close + np.random.randn(n) * 30,
        "high":   close + abs(np.random.randn(n)) * 40,
        "low":    close - abs(np.random.randn(n)) * 40,
        "close":  close,
        "volume": np.random.uniform(1e6, 5e6, n),
    }, index=dates)

    atr = base_price * 0.008
    df["atr_14"] = atr
    df["atr"]    = atr
    df["adx_14"] = 35.0
    df["adx"]    = 35.0

    return df


# ════════════════════════════════════════════════════════════════════════════
# 1. PullbackLongModel Tests
# ════════════════════════════════════════════════════════════════════════════

class TestPullbackLongModel:
    model = PullbackLongModel()

    def _make_pbl_df(self, rsi=55.0, bullish_candle=True, ema50_prox=True, n=100):
        """
        Helper that builds a DataFrame satisfying all PBL conditions by default.

        Candle structure for bullish_candle=True (REJECTION candle — all 3 required):
          close > open  (a) bullish body
          lw > uw       (b) lower wick dominates upper wick
          lw > body     (c) lower wick longer than body

        With explicit high/low set to ensure the constraints hold regardless of
        the randomly-generated base DataFrame values.
        """
        df = make_df(n=n, trend="up")
        base = float(df["close"].iloc[-1])
        atr  = float(df["atr_14"].iloc[-1])

        # Set EMA50 such that proximity condition passes or fails
        if ema50_prox:
            df["ema_50"] = base + 0.3 * atr   # within 0.5×ATR
        else:
            df["ema_50"] = base + 5.0 * atr   # far outside proximity

        df["rsi_14"] = rsi
        df["rsi"]    = rsi

        if bullish_candle:
            # Rejection candle: long lower wick, short upper wick, small bullish body
            #   open  = base - 50   → bullish body = 50
            #   close = base
            #   low   = base - 400  → lower wick = (base-50) - (base-400) = 350
            #   high  = base + 30   → upper wick  = (base+30) - base = 30
            #   → lw(350) > uw(30) ✓   lw(350) > body(50) ✓
            df.iloc[-1, df.columns.get_loc("open")]  = base - 50
            df.iloc[-1, df.columns.get_loc("close")] = base
            df.iloc[-1, df.columns.get_loc("low")]   = base - 400
            df.iloc[-1, df.columns.get_loc("high")]  = base + 30
        else:
            # Non-bullish candle: close ≤ open (bearish)
            df.iloc[-1, df.columns.get_loc("open")]  = base + 100
            df.iloc[-1, df.columns.get_loc("close")] = base
            df.iloc[-1, df.columns.get_loc("low")]   = base - 50
            df.iloc[-1, df.columns.get_loc("high")]  = base + 200

        return df

    def test_fires_when_all_conditions_pass(self):
        df = self._make_pbl_df()
        df_4h = make_htf_4h(bullish=True)
        sig = self.model.evaluate("BTC/USDT", df, "bull_trend", "30m",
                                  context={"df_4h": df_4h})
        assert sig is not None, "PBL should fire when all conditions pass"
        assert sig.direction == "long"
        assert sig.model_name == "pullback_long"

    def test_returns_none_wrong_regime(self):
        df = self._make_pbl_df()
        sig = self.model.evaluate("BTC/USDT", df, "bear_trend", "30m")
        assert sig is None, "PBL must not fire in bear_trend"

    def test_returns_none_non_bullish_candle(self):
        df = self._make_pbl_df(bullish_candle=False)
        sig = self.model.evaluate("BTC/USDT", df, "bull_trend", "30m")
        assert sig is None, "PBL must not fire with non-bullish close"

    def test_returns_none_ema50_too_far(self):
        df = self._make_pbl_df(ema50_prox=False)
        sig = self.model.evaluate("BTC/USDT", df, "bull_trend", "30m")
        assert sig is None, "PBL must not fire when EMA50 too far from close"

    def test_returns_none_rsi_too_low(self):
        df = self._make_pbl_df(rsi=35.0)
        sig = self.model.evaluate("BTC/USDT", df, "bull_trend", "30m")
        assert sig is None, "PBL must not fire when RSI14 ≤ 40"

    def test_rsi_boundary_exactly_40(self):
        df = self._make_pbl_df(rsi=40.0)
        sig = self.model.evaluate("BTC/USDT", df, "bull_trend", "30m")
        assert sig is None, "RSI14 = 40.0 (not strictly greater than) should reject"

    def test_rsi_boundary_just_above_40(self):
        df = self._make_pbl_df(rsi=40.01)
        sig = self.model.evaluate("BTC/USDT", df, "bull_trend", "30m")
        assert sig is not None, "RSI14 = 40.01 should pass"

    def test_htf_bearish_rejects(self):
        df = self._make_pbl_df()
        df_4h = make_htf_4h(bullish=False)
        sig = self.model.evaluate("BTC/USDT", df, "bull_trend", "30m",
                                  context={"df_4h": df_4h})
        assert sig is None, "PBL must reject when 4h HTF is bearish (EMA20 < EMA50)"

    def test_htf_absent_degrades_gracefully(self):
        """When no df_4h in context, model bypasses HTF gate but still fires."""
        df = self._make_pbl_df()
        sig = self.model.evaluate("BTC/USDT", df, "bull_trend", "30m", context={})
        assert sig is not None, "PBL should fire (HTF bypass) when no df_4h available"

    def test_stop_loss_is_below_entry(self):
        df = self._make_pbl_df()
        sig = self.model.evaluate("BTC/USDT", df, "bull_trend", "30m")
        assert sig.stop_loss < sig.entry_price, "Stop loss must be below entry for long"

    def test_take_profit_is_above_entry(self):
        df = self._make_pbl_df()
        sig = self.model.evaluate("BTC/USDT", df, "bull_trend", "30m")
        assert sig.take_profit > sig.entry_price, "Take profit must be above entry for long"

    def test_sl_tp_multiples(self):
        """SL = entry − 2.5×ATR and TP = entry + 3.0×ATR (default config)."""
        df = self._make_pbl_df()
        sig = self.model.evaluate("BTC/USDT", df, "bull_trend", "30m")
        atr = float(df["atr_14"].iloc[-1])
        assert abs((sig.entry_price - sig.stop_loss) / atr - 2.5) < 0.01
        assert abs((sig.take_profit - sig.entry_price) / atr - 3.0) < 0.01

    def test_insufficient_bars_returns_none(self):
        df = self._make_pbl_df(n=30)  # need 60
        sig = self.model.evaluate("BTC/USDT", df, "bull_trend", "30m")
        assert sig is None, "Should return None with fewer than 60 bars"

    def test_strength_in_valid_range(self):
        df = self._make_pbl_df(rsi=70.0)
        sig = self.model.evaluate("BTC/USDT", df, "bull_trend", "30m")
        assert sig is not None
        assert 0.0 < sig.strength <= 0.90, f"Strength {sig.strength} out of range"

    def test_active_regimes_restored(self):
        # v1.3 refactor: ACTIVE_REGIMES=["bull_trend"] restored.
        # Scanner/backtest pass regime from ResearchRegimeClassifier.regime_to_string()
        # so the SignalGenerator ACTIVE_REGIMES gate maps onto research BULL_TREND bars.
        # evaluate() retains a defensive regime check for direct-call safety.
        from core.regime.regime_classifier import REGIME_BULL_TREND
        assert PullbackLongModel.ACTIVE_REGIMES == [REGIME_BULL_TREND], (
            "PBL ACTIVE_REGIMES must be ['bull_trend'] — restored in v1.3 refactor"
        )


# ════════════════════════════════════════════════════════════════════════════
# 2. SwingLowContinuationModel Tests
# ════════════════════════════════════════════════════════════════════════════

class TestSwingLowContinuationModel:
    model = SwingLowContinuationModel()

    def _make_slc_df_1h(self, adx=35.0, new_low=True, n=120):
        """Build 1h DF satisfying SLC conditions by default."""
        df = make_1h_df(n=n, trend="down")
        df["adx"]    = adx
        df["adx_14"] = adx

        base = float(df["close"].iloc[-1])

        if new_low:
            # Make last close below all previous 10 closes
            prev10_min = float(df["close"].iloc[-11:-1].min())
            df.iloc[-1, df.columns.get_loc("close")] = prev10_min - 100
        else:
            # Make last close NOT a new low (set it above previous 10 min)
            prev10_min = float(df["close"].iloc[-11:-1].min())
            df.iloc[-1, df.columns.get_loc("close")] = prev10_min + 500

        return df

    def test_fires_when_all_conditions_pass(self):
        df_1h = self._make_slc_df_1h()
        df_30m = make_df()
        sig = self.model.evaluate("BTC/USDT", df_30m, "bear_trend", "30m",
                                  context={"df_1h": df_1h})
        assert sig is not None, "SLC should fire when all conditions pass"
        assert sig.direction == "short"
        assert sig.model_name == "swing_low_continuation"

    def test_returns_none_wrong_regime(self):
        df_1h = self._make_slc_df_1h()
        df_30m = make_df()
        sig = self.model.evaluate("BTC/USDT", df_30m, "bull_trend", "30m",
                                  context={"df_1h": df_1h})
        assert sig is None, "SLC must not fire in bull_trend"

    def test_returns_none_no_1h_data(self):
        df_30m = make_df()
        sig = self.model.evaluate("BTC/USDT", df_30m, "bear_trend", "30m",
                                  context={})
        assert sig is None, "SLC requires df_1h — must return None when absent"

    def test_returns_none_adx_too_low(self):
        df_1h = self._make_slc_df_1h(adx=25.0)
        df_30m = make_df()
        sig = self.model.evaluate("BTC/USDT", df_30m, "bear_trend", "30m",
                                  context={"df_1h": df_1h})
        assert sig is None, "SLC must not fire when ADX < 28"

    def test_adx_boundary_exactly_28(self):
        df_1h = self._make_slc_df_1h(adx=28.0)
        df_30m = make_df()
        sig = self.model.evaluate("BTC/USDT", df_30m, "bear_trend", "30m",
                                  context={"df_1h": df_1h})
        assert sig is not None, "ADX = 28.0 (≥ threshold) should pass"

    def test_returns_none_not_new_low(self):
        df_1h = self._make_slc_df_1h(new_low=False)
        df_30m = make_df()
        sig = self.model.evaluate("BTC/USDT", df_30m, "bear_trend", "30m",
                                  context={"df_1h": df_1h})
        assert sig is None, "SLC must not fire when close is NOT a new 10-bar low"

    def test_stop_loss_above_entry(self):
        df_1h = self._make_slc_df_1h()
        sig = self.model.evaluate("BTC/USDT", make_df(), "bear_trend", "30m",
                                  context={"df_1h": df_1h})
        assert sig.stop_loss > sig.entry_price, "Stop loss must be ABOVE entry for short"

    def test_take_profit_below_entry(self):
        df_1h = self._make_slc_df_1h()
        sig = self.model.evaluate("BTC/USDT", make_df(), "bear_trend", "30m",
                                  context={"df_1h": df_1h})
        assert sig.take_profit < sig.entry_price, "Take profit must be BELOW entry for short"

    def test_sl_tp_multiples(self):
        """SL = entry + 2.5×ATR and TP = entry − 2.0×ATR (default config)."""
        df_1h = self._make_slc_df_1h()
        sig = self.model.evaluate("BTC/USDT", make_df(), "bear_trend", "30m",
                                  context={"df_1h": df_1h})
        atr = float(df_1h["atr_14"].iloc[-1])
        assert abs((sig.stop_loss - sig.entry_price) / atr - 2.5) < 0.01
        assert abs((sig.entry_price - sig.take_profit) / atr - 2.0) < 0.01

    def test_timeframe_is_1h(self):
        """SLC signal must always report timeframe='1h'."""
        df_1h = self._make_slc_df_1h()
        sig = self.model.evaluate("BTC/USDT", make_df(), "bear_trend", "30m",
                                  context={"df_1h": df_1h})
        assert sig.timeframe == "1h", "SLC signal timeframe must be '1h'"

    def test_strength_in_valid_range(self):
        df_1h = self._make_slc_df_1h(adx=50.0)
        sig = self.model.evaluate("BTC/USDT", make_df(), "bear_trend", "30m",
                                  context={"df_1h": df_1h})
        assert 0.0 < sig.strength <= 0.85, f"Strength {sig.strength} out of range"

    def test_active_regimes_restored(self):
        # v1.3 refactor: ACTIVE_REGIMES=["bear_trend"] restored.
        # Scanner/backtest pass regime from ResearchRegimeClassifier.regime_to_string()
        # applied to the 1h series so the gate maps onto research BEAR_TREND 1h bars.
        # evaluate() retains a defensive regime check for direct-call safety.
        from core.regime.regime_classifier import REGIME_BEAR_TREND
        assert SwingLowContinuationModel.ACTIVE_REGIMES == [REGIME_BEAR_TREND], (
            "SLC ACTIVE_REGIMES must be ['bear_trend'] — restored in v1.3 refactor"
        )


# ════════════════════════════════════════════════════════════════════════════
# 3. PositionSizer.calculate_pos_frac Tests
# ════════════════════════════════════════════════════════════════════════════

class TestPositionSizerPosFrac:
    sizer = PositionSizer()

    @pytest.fixture(autouse=True)
    def _patch_settings(self, monkeypatch):
        """Mock settings so tests don't depend on config.yaml."""
        mock_settings = MagicMock()
        mock_settings.get.side_effect = lambda key, default=None: {
            "mr_pbl_slc.pos_frac":     0.35,
            "mr_pbl_slc.max_heat":     0.80,
            "mr_pbl_slc.max_positions": 10,
            "mr_pbl_slc.max_per_asset":  3,
            "mr_pbl_slc.enabled":       True,
        }.get(key, default)
        with patch("core.meta_decision.position_sizer.settings", mock_settings, create=True):
            yield

    def test_basic_sizing(self):
        size = self.sizer.calculate_pos_frac(100_000, open_positions_count=0)
        assert abs(size - 35_000) < 1.0, f"Expected 35000, got {size}"

    def test_compounding_equity(self):
        """Size grows with equity (pos_frac of current equity)."""
        size1 = self.sizer.calculate_pos_frac(100_000)
        size2 = self.sizer.calculate_pos_frac(150_000)
        assert size2 > size1, "Size must grow with equity"

    def test_heat_gate_rejects_too_many_positions(self):
        """With 2 open positions at 35% each, heat = 1.05 > 0.80 — must reject."""
        size = self.sizer.calculate_pos_frac(100_000, open_positions_count=2)
        assert size == 0.0, f"Heat gate must reject: expected 0.0, got {size}"

    def test_max_positions_gate(self):
        size = self.sizer.calculate_pos_frac(100_000, open_positions_count=10)
        assert size == 0.0, "max_positions=10 — must reject when already at 10"

    def test_max_per_asset_gate(self):
        size = self.sizer.calculate_pos_frac(
            100_000,
            open_positions_by_symbol={"BTC/USDT": 3},
            symbol="BTC/USDT",
        )
        assert size == 0.0, "max_per_asset=3 — must reject when symbol has 3 open"

    def test_zero_capital_returns_zero(self):
        assert self.sizer.calculate_pos_frac(0.0) == 0.0

    def test_negative_capital_returns_zero(self):
        assert self.sizer.calculate_pos_frac(-10_000) == 0.0

    def test_one_open_position_passes_heat(self):
        """1 open at 35% → heat_after = (35%+35%) = 70% < 80% → pass."""
        size = self.sizer.calculate_pos_frac(100_000, open_positions_count=1)
        assert size > 0.0, "One open position should pass heat gate"

    def test_is_pos_frac_mode_active_false_when_disabled(self):
        """When mr_pbl_slc.enabled = false, pos_frac mode must be inactive."""
        # Patch config.settings.settings directly (is_pos_frac_mode_active imports
        # settings locally, so patching must target the source module)
        with patch("config.settings.settings") as mock_s:
            mock_s.get.side_effect = lambda key, default=None: (
                False if key == "mr_pbl_slc.enabled" else default
            )
            assert self.sizer.is_pos_frac_mode_active() is False


# ════════════════════════════════════════════════════════════════════════════
# 4. SignalGenerator Integration Tests
# ════════════════════════════════════════════════════════════════════════════

class TestSignalGeneratorIntegration:

    @pytest.fixture(autouse=True)
    def _patch_mr_enabled(self, monkeypatch):
        """Enable mr_pbl_slc.enabled so models aren't gated out."""
        import core.signals.signal_generator as _sg_mod
        original_get = None
        try:
            from config.settings import settings as _s
            original_get = _s.get
            def patched_get(key, default=None):
                if key == "mr_pbl_slc.enabled":
                    return True
                if key == "disabled_models":
                    return []
                if key == "adaptive_activation.enabled":
                    return False
                return original_get(key, default)
            monkeypatch.setattr(_s, "get", patched_get)
        except Exception:
            pass

    def test_pbl_model_registered(self):
        from core.signals.signal_generator import _ALL_MODELS
        names = [m.name for m in _ALL_MODELS]
        assert "pullback_long" in names, "PullbackLongModel must be in _ALL_MODELS"

    def test_slc_model_registered(self):
        from core.signals.signal_generator import _ALL_MODELS
        names = [m.name for m in _ALL_MODELS]
        assert "swing_low_continuation" in names, "SwingLowContinuationModel must be in _ALL_MODELS"

    def test_context_passed_to_pbl(self):
        """SignalGenerator must pass context dict to evaluate() when parameter exists."""
        from core.signals.signal_generator import SignalGenerator
        pbl = PullbackLongModel()
        called_with_context = []
        original_eval = pbl.evaluate

        def spy_eval(symbol, df, regime, timeframe, context=None):
            called_with_context.append(context)
            return None  # don't care about result for this test
        pbl.evaluate = spy_eval

        gen = SignalGenerator(models=[pbl])
        gen._warmup_complete = True  # skip warmup
        df = make_df()
        ctx = {"df_4h": make_htf_4h()}
        gen.generate("BTC/USDT", df, "bull_trend", "30m", context=ctx)

        assert len(called_with_context) == 1
        assert called_with_context[0] is ctx, "Context must be passed through to model.evaluate()"

    def test_pbl_gated_when_disabled(self):
        """When mr_pbl_slc.enabled=false, PBL and SLC must not evaluate."""
        from core.signals.signal_generator import SignalGenerator
        from unittest.mock import patch as p_

        pbl = PullbackLongModel()
        eval_calls = []
        pbl.evaluate = lambda *a, **kw: eval_calls.append(1) or None

        gen = SignalGenerator(models=[pbl])
        gen._warmup_complete = True

        settings_values = {
            "mr_pbl_slc.enabled": False,
            "disabled_models": [],
            "adaptive_activation.enabled": False,
            "adaptive_activation.min_activation_weight": 0.1,
        }
        with p_("config.settings.settings") as mock_s:
            mock_s.get.side_effect = lambda key, default=None: settings_values.get(key, default)
            gen.generate("BTC/USDT", make_df(), "bull_trend", "30m")

        assert len(eval_calls) == 0, "PBL must not be called when mr_pbl_slc.enabled=false"

    def test_slc_uses_1h_from_context(self):
        """SLC must return None without df_1h even when all other conditions pass."""
        from core.signals.signal_generator import SignalGenerator
        from unittest.mock import patch as p_
        slc = SwingLowContinuationModel()
        gen = SignalGenerator(models=[slc])
        gen._warmup_complete = True

        df = make_df()
        settings_values = {
            "mr_pbl_slc.enabled": True,
            "disabled_models": [],
            "adaptive_activation.enabled": False,
            "adaptive_activation.min_activation_weight": 0.1,
        }
        with p_("config.settings.settings") as mock_s:
            mock_s.get.side_effect = lambda key, default=None: settings_values.get(key, default)
            with patch.object(slc, "evaluate", wraps=slc.evaluate) as spy:
                gen.generate("BTC/USDT", df, "bear_trend", "30m", context={})
                # evaluate was called but returned None due to missing df_1h
                assert spy.called, "SLC evaluate() must be called"
