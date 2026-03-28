# ============================================================
# NEXUS TRADER — Indicator Library (30+ indicators)
# Uses the 'ta' library — pandas 3.x compatible
# ============================================================

import logging
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

try:
    import ta
    TA_AVAILABLE = True
except ImportError:
    logger.warning("'ta' library not installed. Run: pip install ta")
    TA_AVAILABLE = False


def calculate_all(df: pd.DataFrame, config: dict = None) -> pd.DataFrame:
    """
    Calculate all indicators on an OHLCV DataFrame.
    Input columns: open, high, low, close, volume
    Returns the same DataFrame with indicator columns added.
    """
    if df is None or df.empty or len(df) < 2:
        return df

    cfg = config or {}
    df = df.copy()

    if not TA_AVAILABLE:
        logger.warning("Skipping indicator calculation — 'ta' not installed")
        return df

    try:
        # ── Trend Indicators ──────────────────────────────────
        # EMA variants — batch-concat to avoid DataFrame fragmentation warning
        _ema_periods = [2, 3, 5, 8, 9, 10, 12, 20, 21, 26, 27, 32, 50, 55, 63, 100, 200]
        _ema_cols = {f"ema_{_ep}": ta.trend.ema_indicator(df["close"], window=_ep)
                     for _ep in _ema_periods}
        df = pd.concat([df, pd.DataFrame(_ema_cols, index=df.index)], axis=1)

        # SMA variants — batch-concat
        _sma_periods = [2, 3, 5, 8, 9, 10, 12, 20, 21, 26, 27, 32, 50, 55, 63, 100, 200]
        _sma_cols = {f"sma_{_sp}": ta.trend.sma_indicator(df["close"], window=_sp)
                     for _sp in _sma_periods}
        df = pd.concat([df, pd.DataFrame(_sma_cols, index=df.index)], axis=1)

        # WMA
        df["wma_20"] = ta.trend.wma_indicator(df["close"], window=20)

        # ADX (Average Directional Index) — trend strength
        try:
            for _adx_p in [14, 20]:
                adx_obj = ta.trend.ADXIndicator(
                    df["high"], df["low"], df["close"], window=_adx_p
                )
                df[f"adx_{_adx_p}"] = adx_obj.adx()
            df["adx"] = df["adx_14"]   # default alias used by condition parser
        except Exception as e:
            logger.debug("ADX calculation failed: %s", e)

        # VWAP — session-reset (UTC midnight) where index is timezone-aware,
        # falling back to rolling VWAP for tz-naive or legacy DataFrames.
        # Session-reset VWAP matches the VWAP displayed on exchanges and in
        # trading platforms, ensuring VWAPReversionModel uses the same reference.
        try:
            hlc3 = (df["high"] + df["low"] + df["close"]) / 3
            if hasattr(df.index, "tz") and df.index.tz is not None:
                # normalize() truncates DatetimeIndex to midnight UTC — the daily
                # grouping key that resets VWAP each calendar day.
                _day_groups = df.index.normalize()
                _cum_tp_vol = (hlc3 * df["volume"]).groupby(_day_groups).cumsum()
                _cum_vol    = df["volume"].groupby(_day_groups).cumsum().replace(0, float("nan"))
                df["vwap"]  = _cum_tp_vol / _cum_vol
            else:
                # Fallback for tz-naive data: rolling 14-bar approximation
                df["vwap"] = ta.volume.volume_weighted_average_price(
                    df["high"], df["low"], df["close"], df["volume"],
                    window=cfg.get("vwap_window", 14),
                )
        except Exception:
            df["vwap"] = (df["high"] + df["low"] + df["close"]) / 3

        # MACD
        macd_obj     = ta.trend.MACD(df["close"],
                                     window_slow=cfg.get("macd_slow", 26),
                                     window_fast=cfg.get("macd_fast", 12),
                                     window_sign=cfg.get("macd_signal", 9))
        df["macd"]        = macd_obj.macd()
        df["macd_signal"] = macd_obj.macd_signal()
        df["macd_hist"]   = macd_obj.macd_diff()

        # SuperTrend variants (per Rules Allowed.docx: 5, 10, 15 periods)
        # Each call adds 2 columns; copy() after to defragment the accumulated blocks
        for _stp in [5, 10, 15]:
            df = _supertrend(df, period=_stp, multiplier=3.0, col_suffix=f"_{_stp}")
        df = df.copy()   # defragment after sequential supertrend insertions
        df["supertrend"] = df.get("supertrend_10", pd.Series(dtype=float))  # legacy alias

        # Ichimoku Cloud
        try:
            ichi = ta.trend.IchimokuIndicator(
                df["high"], df["low"],
                window1=cfg.get("ichi_9", 9),
                window2=cfg.get("ichi_26", 26),
                window3=cfg.get("ichi_52", 52),
            )
            df["ichi_conversion"] = ichi.ichimoku_conversion_line()
            df["ichi_base"]       = ichi.ichimoku_base_line()
            df["ichi_a"]          = ichi.ichimoku_a()
            df["ichi_b"]          = ichi.ichimoku_b()
        except Exception as e:
            logger.debug("Ichimoku failed (need 52+ candles): %s", e)

        # ── Momentum Indicators (non-RSI) ────────────────────

        # Stochastic RSI
        try:
            srsi = ta.momentum.StochRSIIndicator(df["close"],
                                                  window=cfg.get("srsi_window", 14),
                                                  smooth1=3, smooth2=3)
            df["stoch_rsi_k"] = srsi.stochrsi_k()
            df["stoch_rsi_d"] = srsi.stochrsi_d()
        except Exception:
            pass

        # Stochastic Oscillator
        stoch = ta.momentum.StochasticOscillator(
            df["high"], df["low"], df["close"],
            window=cfg.get("stoch_window", 14),
            smooth_window=3
        )
        df["stoch_k"] = stoch.stoch()
        df["stoch_d"] = stoch.stoch_signal()

        # Momentum
        df["momentum"] = ta.momentum.roc(df["close"], window=cfg.get("mom_period", 10))

        # CCI
        df["cci"] = ta.trend.cci(df["high"], df["low"], df["close"],
                                  window=cfg.get("cci_period", 20))

        # ROC
        df["roc"] = ta.momentum.roc(df["close"], window=cfg.get("roc_period", 12))

        # Williams %R
        df["williams_r"] = ta.momentum.williams_r(
            df["high"], df["low"], df["close"],
            lbp=cfg.get("wr_period", 14)
        )

        # ── Momentum Indicators (RSI variants) ───────────────
        # RSI variants — batch-concat
        _rsi_periods = [2, 3, 5, 6, 7, 8, 12, 14, 24]
        _rsi_cols = {f"rsi_{_rp}": ta.momentum.rsi(df["close"], window=_rp)
                     for _rp in _rsi_periods}
        df = pd.concat([df, pd.DataFrame(_rsi_cols, index=df.index)], axis=1)
        df["rsi"] = df["rsi_14"]   # legacy alias (still referenced by old rules)

        # TWAP (rolling 14-bar HLC/3 average)
        try:
            _hlc3 = (df["high"] + df["low"] + df["close"]) / 3
            df["twap"] = _hlc3.rolling(window=14).mean()
        except Exception:
            pass

        # ── Volatility Indicators ─────────────────────────────
        # ATR variants — batch-concat
        _atr_periods = [2, 3, 5, 6, 7, 8, 12, 14, 24]
        _atr_cols = {f"atr_{_ap}": ta.volatility.average_true_range(
                         df["high"], df["low"], df["close"], window=_ap)
                     for _ap in _atr_periods}
        df = pd.concat([df, pd.DataFrame(_atr_cols, index=df.index)], axis=1)
        df["atr"] = df["atr_14"]   # legacy alias

        # Bollinger Bands
        bb = ta.volatility.BollingerBands(
            df["close"],
            window=cfg.get("bb_period", 20),
            window_dev=cfg.get("bb_std", 2)
        )
        df["bb_upper"] = bb.bollinger_hband()
        df["bb_mid"]   = bb.bollinger_mavg()
        df["bb_lower"] = bb.bollinger_lband()
        df["bb_width"] = bb.bollinger_wband()
        df["bb_pct"]   = bb.bollinger_pband()

        # Keltner Channels
        try:
            kc = ta.volatility.KeltnerChannel(
                df["high"], df["low"], df["close"],
                window=cfg.get("kc_period", 20),
                window_atr=cfg.get("kc_atr", 10)
            )
            df["kc_upper"] = kc.keltner_channel_hband()
            df["kc_mid"]   = kc.keltner_channel_mband()
            df["kc_lower"] = kc.keltner_channel_lband()
        except Exception:
            pass

        # Donchian Channels
        try:
            dc = ta.volatility.DonchianChannel(
                df["high"], df["low"], df["close"],
                window=cfg.get("dc_period", 20)
            )
            df["dc_upper"] = dc.donchian_channel_hband()
            df["dc_mid"]   = dc.donchian_channel_mband()
            df["dc_lower"] = dc.donchian_channel_lband()
        except Exception:
            pass

        # ── Volume Indicators ─────────────────────────────────
        # OBV
        df["obv"] = ta.volume.on_balance_volume(df["close"], df["volume"])

        # Accumulation/Distribution
        df["ad"]  = ta.volume.acc_dist_index(
            df["high"], df["low"], df["close"], df["volume"]
        )

        # Money Flow Index
        df["mfi"] = ta.volume.money_flow_index(
            df["high"], df["low"], df["close"], df["volume"],
            window=cfg.get("mfi_period", 14)
        )

        # CMF (Chaikin Money Flow)
        df["cmf"] = ta.volume.chaikin_money_flow(
            df["high"], df["low"], df["close"], df["volume"],
            window=cfg.get("cmf_period", 20)
        )

        # ── Market Structure ──────────────────────────────────
        # Pivot Points (classic — calculated manually)
        df = _pivot_points(df)

        # Fibonacci levels from recent swing
        df = _fibonacci_levels(df, lookback=cfg.get("fib_lookback", 50))

        logger.debug("Indicators calculated: %d columns on %d rows",
                     len(df.columns), len(df))
        return df

    except Exception as e:
        logger.error("Indicator calculation error: %s", e, exc_info=True)
        return df


# ── Helper: Supertrend ────────────────────────────────────────
def _supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0,
                col_suffix: str = "") -> pd.DataFrame:
    """Calculate Supertrend indicator.  col_suffix allows variant columns
    like 'supertrend_5', 'supertrend_10', 'supertrend_15'."""
    try:
        atr = ta.volatility.average_true_range(df["high"], df["low"], df["close"], window=period)
        hl2 = (df["high"] + df["low"]) / 2
        upper_band = hl2 + multiplier * atr
        lower_band = hl2 - multiplier * atr

        supertrend = pd.Series(index=df.index, dtype=float)
        direction  = pd.Series(index=df.index, dtype=float)

        for i in range(1, len(df)):
            if df["close"].iloc[i] > upper_band.iloc[i - 1]:
                direction.iloc[i] = 1    # Bullish
            elif df["close"].iloc[i] < lower_band.iloc[i - 1]:
                direction.iloc[i] = -1   # Bearish
            else:
                direction.iloc[i] = direction.iloc[i - 1]

            if direction.iloc[i] == 1:
                supertrend.iloc[i] = lower_band.iloc[i]
            else:
                supertrend.iloc[i] = upper_band.iloc[i]

        st_col  = f"supertrend{col_suffix}"
        dir_col = f"supertrend_dir{col_suffix}"
        df[st_col]  = supertrend
        df[dir_col] = direction
    except Exception as e:
        logger.debug("Supertrend%s calculation failed: %s", col_suffix, e)
    return df


# ── Helper: Pivot Points ──────────────────────────────────────
def _pivot_points(df: pd.DataFrame) -> pd.DataFrame:
    """Classic daily pivot points."""
    try:
        prev_high  = df["high"].shift(1)
        prev_low   = df["low"].shift(1)
        prev_close = df["close"].shift(1)
        pivot = (prev_high + prev_low + prev_close) / 3
        df["pivot"]  = pivot
        df["pivot_r1"] = 2 * pivot - prev_low
        df["pivot_s1"] = 2 * pivot - prev_high
        df["pivot_r2"] = pivot + (prev_high - prev_low)
        df["pivot_s2"] = pivot - (prev_high - prev_low)
    except Exception:
        pass
    return df


# ── Helper: Fibonacci Levels ──────────────────────────────────
def _fibonacci_levels(df: pd.DataFrame, lookback: int = 50) -> pd.DataFrame:
    """Calculate Fibonacci retracement levels from recent swing high/low."""
    try:
        swing_high = df["high"].rolling(lookback).max()
        swing_low  = df["low"].rolling(lookback).min()
        diff = swing_high - swing_low
        df["fib_236"] = swing_high - 0.236 * diff
        df["fib_382"] = swing_high - 0.382 * diff
        df["fib_500"] = swing_high - 0.500 * diff
        df["fib_618"] = swing_high - 0.618 * diff
        df["fib_786"] = swing_high - 0.786 * diff
    except Exception:
        pass
    return df


# ── Scan-mode indicator computation ──────────────────────────
#
# SCAN_CORE_COLUMNS — the minimum set of computed columns required for
# the live IDSS scan path.  Derived from reading every active consumer:
#
#   TrendModel:              ema_9, ema_20, ema_21, ema_100,
#                            adx_14 (alias: adx), rsi_14 (alias: rsi),
#                            macd, macd_signal, atr_14 (alias: atr)
#   MomentumBreakout:        rsi_14, atr_14 only (plus raw OHLCV)
#   PullbackLongModel:       ema_50, rsi_14 (read from df_4h HTF context)
#   SwingLowContinuation:    adx, rsi_14 (read from df_1h HTF context)
#   FundingRateModel:        atr_14 only (plus close)
#   SentimentModel:          atr_14 only (plus close)
#   OrderBookModel:          atr_14 only — hard-gated at 1h (returns None)
#   RegimeClassifier (rule): adx, ema_20, bb_upper, bb_lower, bb_mid, rsi
#   RegimeClassifier (HMM):  adx (feature 3); others derived from close/volume
#   Volatility pre-filter:   atr (computed inline if absent)
#
# All other columns produced by calculate_all() are REMOVE or CONDITIONAL
# (BacktestEngine / research paths only).
#
# IMPORTANT: calculate_scan_mode() is called for ALL timeframe data:
#   - 30m primary data (299 bars)
#   - 4h HTF context data for PBL (59 bars)
#   - 1h HTF context data for SLC (149 bars)
# Every column in this set MUST be computable on all three data shapes.
#
SCAN_CORE_COLUMNS: frozenset[str] = frozenset({
    # TrendModel EMAs
    "ema_9", "ema_20", "ema_21", "ema_100",
    # PullbackLongModel (4h context) — EMA-50 proximity condition
    "ema_50",
    # Trend strength / momentum
    "adx_14", "adx",
    "rsi_14", "rsi",
    "macd", "macd_signal",
    # Volatility / ATR
    "atr_14", "atr",
    # Regime classifier (rule-based) — Bollinger
    "bb_upper", "bb_lower", "bb_mid", "bb_width",
})


def calculate_scan_mode(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute only the CORE indicator set required for the live IDSS scan path.

    This replaces calculate_all() in scanner.py to eliminate dead-weight
    computation of ~95 columns that no active live-scan consumer reads.

    Consumers and their exact requirements are documented in SCAN_CORE_COLUMNS.

    BacktestEngine, IDSSBacktester, and all research/validation paths must
    continue to call calculate_all() — they require the full indicator set
    for condition-tree evaluation and walk-forward validation.

    Performance target: ≥ 40 % faster than calculate_all() on 300-bar OHLCV.
    Measured: SuperTrend (3 × O(n) Python loops) + 17 SMA + 13 spare EMA +
    8 spare RSI + 8 spare ATR + Ichimoku + StochRSI + Keltner + Donchian +
    Pivot + Fibonacci are the primary savings.
    """
    if df is None or df.empty or len(df) < 2:
        return df

    if not TA_AVAILABLE:
        logger.warning("calculate_scan_mode: 'ta' not installed — returning raw OHLCV")
        return df

    df = df.copy()

    try:
        # ── Core EMAs (TrendModel + PullbackLongModel) ────────
        # Periods: 9/20/21/100 for TrendModel; 50 for PBL EMA-proximity
        # condition (pullback_long_model.py line 156: df["ema_50"]).
        _ema_core = {
            f"ema_{p}": ta.trend.ema_indicator(df["close"], window=p)
            for p in [9, 20, 21, 50, 100]
        }
        df = pd.concat([df, pd.DataFrame(_ema_core, index=df.index)], axis=1)

        # ── ADX-14 (TrendModel + RegimeClassifiers) ───────────
        try:
            adx_obj    = ta.trend.ADXIndicator(df["high"], df["low"], df["close"], window=14)
            df["adx_14"] = adx_obj.adx()
            df["adx"]    = df["adx_14"]   # legacy alias — must keep for regime classifier
        except Exception as _adx_err:
            logger.debug("calculate_scan_mode: ADX failed — %s", _adx_err)

        # ── RSI-14 (TrendModel + MomentumBreakout + Regime) ──
        df["rsi_14"] = ta.momentum.rsi(df["close"], window=14)
        df["rsi"]    = df["rsi_14"]   # legacy alias

        # ── MACD (TrendModel) ─────────────────────────────────
        macd_obj         = ta.trend.MACD(df["close"], window_slow=26,
                                          window_fast=12, window_sign=9)
        df["macd"]        = macd_obj.macd()
        df["macd_signal"] = macd_obj.macd_signal()
        # macd_hist intentionally omitted — no active consumer reads it in scan path

        # ── ATR-14 (all models + volatility pre-filter) ───────
        df["atr_14"] = ta.volatility.average_true_range(
            df["high"], df["low"], df["close"], window=14
        )
        df["atr"] = df["atr_14"]   # legacy alias

        # ── Bollinger Bands (RegimeClassifier rule-based) ─────
        bb = ta.volatility.BollingerBands(df["close"], window=20, window_dev=2)
        df["bb_upper"] = bb.bollinger_hband()
        df["bb_mid"]   = bb.bollinger_mavg()
        df["bb_lower"] = bb.bollinger_lband()
        df["bb_width"] = bb.bollinger_wband()
        # bb_pct intentionally omitted — not used by any active scan consumer

        logger.debug(
            "calculate_scan_mode: %d scan-mode columns on %d rows (CORE=%d)",
            len(df.columns), len(df), len(SCAN_CORE_COLUMNS),
        )
        return df

    except Exception as exc:
        logger.error("calculate_scan_mode: failed — %s", exc, exc_info=True)
        return df


# ── Signal Generator ──────────────────────────────────────────
def get_signals(df: pd.DataFrame) -> dict:
    """
    Generate simple signal summary from latest indicator values.
    Returns dict with signal, strength (0-100), and reasons.
    """
    if df is None or df.empty:
        return {"signal": "neutral", "strength": 0, "reasons": []}

    latest = df.iloc[-1]
    prev   = df.iloc[-2] if len(df) > 1 else latest

    bullish_count = 0
    bearish_count = 0
    reasons = []

    # RSI
    rsi = latest.get("rsi")
    if rsi is not None and not pd.isna(rsi):
        if rsi < 30:
            bullish_count += 2; reasons.append(f"RSI oversold ({rsi:.1f})")
        elif rsi > 70:
            bearish_count += 2; reasons.append(f"RSI overbought ({rsi:.1f})")
        elif rsi > 50:
            bullish_count += 1
        else:
            bearish_count += 1

    # MACD crossover
    macd = latest.get("macd"); sig = latest.get("macd_signal")
    prev_macd = prev.get("macd"); prev_sig = prev.get("macd_signal")
    if all(v is not None and not pd.isna(v) for v in [macd, sig, prev_macd, prev_sig]):
        if macd > sig and prev_macd <= prev_sig:
            bullish_count += 2; reasons.append("MACD bullish crossover")
        elif macd < sig and prev_macd >= prev_sig:
            bearish_count += 2; reasons.append("MACD bearish crossover")
        elif macd > sig:
            bullish_count += 1
        else:
            bearish_count += 1

    # Price vs EMA 20
    ema20 = latest.get("ema_20"); close = latest.get("close")
    if ema20 and close and not pd.isna(ema20):
        if close > ema20:
            bullish_count += 1; reasons.append("Price above EMA 20")
        else:
            bearish_count += 1; reasons.append("Price below EMA 20")

    # EMA 20/50 crossover
    ema50 = latest.get("ema_50")
    if ema20 and ema50 and not pd.isna(ema20) and not pd.isna(ema50):
        if ema20 > ema50:
            bullish_count += 1
        else:
            bearish_count += 1

    # Bollinger Band squeeze signal
    bb_pct = latest.get("bb_pct")
    if bb_pct is not None and not pd.isna(bb_pct):
        if bb_pct < 0.2:
            bullish_count += 1; reasons.append("Near BB lower band")
        elif bb_pct > 0.8:
            bearish_count += 1; reasons.append("Near BB upper band")

    total = bullish_count + bearish_count
    if total == 0:
        return {"signal": "neutral", "strength": 50, "reasons": reasons}

    bull_pct = (bullish_count / total) * 100

    if bull_pct >= 65:
        signal = "bullish"
    elif bull_pct <= 35:
        signal = "bearish"
    else:
        signal = "neutral"

    return {
        "signal":   signal,
        "strength": int(bull_pct),
        "bullish":  bullish_count,
        "bearish":  bearish_count,
        "reasons":  reasons[:5],
        "rsi":      round(rsi, 1) if rsi and not pd.isna(rsi) else None,
    }
