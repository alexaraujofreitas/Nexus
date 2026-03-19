# ============================================================
# NEXUS TRADER — Walk-Forward Regime-Segmented Validator
#
# PURPOSE
# ───────
# Evaluate whether NexusTrader demonstrates PERSISTENT edge
# across different market regimes, rather than appearing
# profitable only due to a favorable historical period.
#
# This module is STRICTLY an evaluation framework.
# It does NOT modify trading architecture, model logic, or
# any production code.  It wraps the existing IDSSBacktester
# and WalkForwardValidator with:
#
#   • Enhanced trade records (stop/tp/direction/R-multiple)
#   • Multi-symbol parallel validation
#   • Regime-segmented performance analytics
#   • Rolling stability metrics
#   • Edge persistence analysis
#
# ARCHITECTURE
# ────────────
# SyntheticRegimeDataGenerator  — produces realistic OHLCV with
#                                 labeled regime periods (or wraps
#                                 live OHLCV when provided).
#
# EnhancedIDSSBacktester        — subclass of IDSSBacktester that
#                                 records stop/tp/direction/R in
#                                 trade dicts.  Adds no new logic.
#
# RegimeSegmentedWalkForwardValidator
#                               — orchestrates multi-symbol
#                                 walk-forward, aggregates trades,
#                                 and computes all analytics.
#
# NO LOOK-AHEAD BIAS
# ──────────────────
# Each forward-test window receives only data that would have
# been available at the start of that window.  The calibration
# window is used purely for indicator warm-up, NOT for model
# training (the IDSS models are not retrained — they are
# stateless at the level of the backtester).
# ============================================================
from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Configuration dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class WalkForwardConfig:
    """
    Configuration for the walk-forward regime-segmented validation run.

    Window sizing (bars)
    ────────────────────
    calibration_bars : bars of history fed before each test window
                       (warm-up for indicators and HMM classifier)
    test_bars        : bars in each forward-test window
    step_bars        : how far to advance the window each cycle
    """
    symbols:            list[str]  = field(default_factory=lambda: [
        "BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT"
    ])
    timeframe:          str   = "4h"
    calibration_bars:   int   = 400   # ~67 days of 4h bars
    test_bars:          int   = 200   # ~33 days of 4h bars
    step_bars:          int   = 200   # non-overlapping forward windows
    initial_capital:    float = 10_000.0
    fee_pct:            float = 0.10  # 0.10% per side
    slippage_pct:       float = 0.05  # 0.05% slippage
    spread_pct:         float = 0.05  # 0.05% bid-ask spread
    min_confluence_score: float = 0.45
    warmup_bars:        int   = 100   # IDSS pipeline warm-up


# ─────────────────────────────────────────────────────────────────────────────
# Result dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class WalkForwardResult:
    """Aggregated walk-forward validation results."""
    config:                 WalkForwardConfig

    # ── Per-symbol results ──────────────────────────────────
    symbol_results:         dict[str, dict]   = field(default_factory=dict)

    # ── Aggregated trade dataset ─────────────────────────────
    all_trades:             list[dict]        = field(default_factory=list)

    # ── Walk-forward windows ─────────────────────────────────
    windows:                list[dict]        = field(default_factory=list)

    # ── Global metrics ───────────────────────────────────────
    global_metrics:         dict              = field(default_factory=dict)

    # ── Regime segmentation ──────────────────────────────────
    by_regime:              dict[str, dict]   = field(default_factory=dict)

    # ── Asset segmentation ───────────────────────────────────
    by_asset:               dict[str, dict]   = field(default_factory=dict)

    # ── Model segmentation ───────────────────────────────────
    by_model:               dict[str, dict]   = field(default_factory=dict)

    # ── Score bucket calibration ─────────────────────────────
    by_score_bucket:        dict[str, dict]   = field(default_factory=dict)

    # ── Rolling series (for charts) ──────────────────────────
    cumulative_r_history:   list[float]       = field(default_factory=list)
    rolling_20_exp_history: list[float]       = field(default_factory=list)
    rolling_20_pf_history:  list[float]       = field(default_factory=list)
    rolling_dd_r_history:   list[float]       = field(default_factory=list)

    # ── Equity curve ─────────────────────────────────────────
    equity_curve:           list[float]       = field(default_factory=list)
    equity_timestamps:      list[str]         = field(default_factory=list)

    # ── Edge persistence summary ─────────────────────────────
    edge_verdict:           str  = "NOT_READY"
    edge_explanation:       str  = ""
    generated_at:           str  = ""

    # ── Walk-forward window count ─────────────────────────────
    window_count:           int  = 0
    total_symbols:          int  = 0


# ─────────────────────────────────────────────────────────────────────────────
# Synthetic Regime Data Generator
# ─────────────────────────────────────────────────────────────────────────────

# Regime simulation parameters
_REGIME_PARAMS: dict[str, dict] = {
    "bull_trend":   {"drift": +0.00030, "vol": 0.0080, "atr_mult": 1.20},
    "bear_trend":   {"drift": -0.00050, "vol": 0.0120, "atr_mult": 1.80},
    "ranging":      {"drift":  0.00000, "vol": 0.0050, "atr_mult": 0.70, "mean_revert": 0.02},
    "vol_expansion":{"drift":  0.00010, "vol": 0.0250, "atr_mult": 3.50},
    "vol_compress": {"drift":  0.00000, "vol": 0.0030, "atr_mult": 0.40},
}

# Symbol-specific regime schedules (regime_name, n_bars)
_SYMBOL_SCHEDULES: dict[str, list[tuple[str, int]]] = {
    "BTC/USDT": [
        ("ranging",       150), ("bull_trend",    250), ("vol_expansion",  80),
        ("bear_trend",    180), ("ranging",       120), ("bull_trend",    200),
        ("vol_compress",   80), ("ranging",       140), ("bear_trend",    150),
        ("vol_expansion",  60), ("bull_trend",    190), ("ranging",       100),
    ],
    "ETH/USDT": [
        ("bull_trend",    220), ("ranging",       130), ("vol_expansion",  70),
        ("bear_trend",    200), ("vol_compress",   90), ("bull_trend",    180),
        ("ranging",       150), ("bear_trend",    160), ("vol_expansion",  60),
        ("ranging",       120), ("bull_trend",    170), ("vol_compress",   80),
    ],
    "BNB/USDT": [
        ("vol_compress",   80), ("bull_trend",    200), ("ranging",       160),
        ("bear_trend",    170), ("vol_expansion",  70), ("ranging",       130),
        ("bull_trend",    210), ("vol_compress",   90), ("bear_trend",    180),
        ("ranging",       100), ("vol_expansion",  60), ("bull_trend",    180),
    ],
    "SOL/USDT": [
        ("bull_trend",    260), ("vol_expansion",  90), ("bear_trend",    220),
        ("ranging",       100), ("vol_compress",   70), ("bull_trend",    160),
        ("bear_trend",    180), ("ranging",       140), ("vol_expansion",  70),
        ("bull_trend",    220), ("ranging",       130), ("bear_trend",     90),
    ],
    "XRP/USDT": [
        ("ranging",       200), ("vol_compress",  100), ("bull_trend",    180),
        ("bear_trend",    150), ("ranging",       120), ("vol_expansion",  80),
        ("bull_trend",    200), ("ranging",       160), ("bear_trend",    140),
        ("vol_compress",   70), ("bull_trend",    180), ("ranging",       130),
    ],
}

_BASE_PRICES: dict[str, float] = {
    "BTC/USDT": 42000.0,
    "ETH/USDT": 2200.0,
    "BNB/USDT": 300.0,
    "SOL/USDT": 85.0,
    "XRP/USDT": 0.52,
}


class SyntheticRegimeDataGenerator:
    """
    Generates realistic synthetic OHLCV data with labeled regime periods
    for walk-forward validation.

    Each symbol follows a pre-defined regime schedule that represents
    realistic market dynamics including bull/bear trends, ranging markets,
    volatility expansion/compression cycles.

    The generated data exhibits:
    • Regime-specific volatility and drift
    • Mean-reversion behavior in ranging regimes
    • Volume correlation with volatility
    • Realistic OHLC candle structure (High >= max(Open,Close), etc.)
    """

    def __init__(self, seed: int = 42):
        self._rng = np.random.default_rng(seed)

    def generate(
        self,
        symbol: str,
        timeframe: str = "4h",
        start_date: str = "2024-01-01",
        custom_schedule: Optional[list] = None,
    ) -> tuple[pd.DataFrame, list[tuple[str, int, int]]]:
        """
        Generate synthetic OHLCV DataFrame with regime metadata.

        Returns
        -------
        df : pd.DataFrame
            OHLCV with DatetimeIndex and a 'true_regime' column.
        regime_periods : list of (regime_name, start_bar, end_bar)
            True regime labels for each period.
        """
        schedule = custom_schedule or _SYMBOL_SCHEDULES.get(
            symbol, _SYMBOL_SCHEDULES["BTC/USDT"]
        )
        base_price = _BASE_PRICES.get(symbol, 100.0)

        # Build bar-by-bar price series
        closes: list[float]  = []
        opens:  list[float]  = []
        highs:  list[float]  = []
        lows:   list[float]  = []
        vols:   list[float]  = []
        regimes: list[str]   = []
        regime_periods: list[tuple[str, int, int]] = []

        price = base_price
        bar_idx = 0

        for regime_name, n_bars in schedule:
            p = _REGIME_PARAMS.get(regime_name, _REGIME_PARAMS["ranging"])
            drift     = p["drift"]
            vol       = p["vol"]
            mean_rev  = p.get("mean_revert", 0.0)
            regime_start = bar_idx

            # Track mean price for mean-reversion regimes
            if mean_rev > 0:
                mean_price = price

            for _ in range(n_bars):
                shock = self._rng.normal(drift, vol)
                if mean_rev > 0:
                    # Mean-reversion pull
                    mean_gap = (mean_price - price) / mean_price
                    shock += mean_rev * mean_gap
                price = price * (1 + shock)
                price = max(price, base_price * 0.01)  # floor

                open_px  = price * (1 + self._rng.normal(0, vol * 0.3))
                high_px  = max(open_px, price) * (1 + abs(self._rng.normal(0, vol * 0.5)))
                low_px   = min(open_px, price) * (1 - abs(self._rng.normal(0, vol * 0.5)))
                high_px  = max(high_px, open_px, price)
                low_px   = min(low_px,  open_px, price)
                volume   = self._rng.uniform(500, 5000) * (1 + 5 * abs(shock / vol))

                opens.append(float(open_px))
                highs.append(float(high_px))
                lows.append(float(low_px))
                closes.append(float(price))
                vols.append(float(volume))
                regimes.append(regime_name)
                bar_idx += 1

            regime_periods.append((regime_name, regime_start, bar_idx - 1))

        # Build freq string for timeframe
        _TF_MAP = {
            "1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min",
            "1h": "1h",   "4h": "4h",   "1d": "1D",
        }
        freq = _TF_MAP.get(timeframe, "4h")

        n_total = len(closes)
        idx = pd.date_range(start_date, periods=n_total, freq=freq)

        df = pd.DataFrame({
            "open":        opens,
            "high":        highs,
            "low":         lows,
            "close":       closes,
            "volume":      vols,
            "true_regime": regimes,
        }, index=idx)

        return df, regime_periods

    def generate_all_symbols(
        self,
        symbols: list[str],
        timeframe: str = "4h",
        start_date: str = "2024-01-01",
    ) -> dict[str, tuple[pd.DataFrame, list]]:
        """Generate data for all symbols. Returns {symbol: (df, regime_periods)}."""
        return {
            sym: self.generate(sym, timeframe, start_date)
            for sym in symbols
        }


# ─────────────────────────────────────────────────────────────────────────────
# Enhanced IDSSBacktester — records extra trade fields
# ─────────────────────────────────────────────────────────────────────────────

class EnhancedIDSSBacktester:
    """
    Drop-in replacement for IDSSBacktester that records additional
    trade fields required for regime-segmented analysis:

      • stop_price, tp_price    — exact SL/TP levels
      • direction               — "long" or "short"
      • symbol                  — trading pair
      • size_usdt               — position value at entry
      • expected_rr             — abs(tp - entry) / abs(sl - entry)
      • realized_r_multiple     — pnl / risk_usdt
      • slippage_estimate_pct   — configured slippage

    This class is STRICTLY an evaluation wrapper.  It does NOT
    modify any IDSS signal logic, regime classification, or risk
    management parameters.
    """

    def __init__(
        self,
        min_confluence_score: float | None = None,
        warmup_bars: int = 100,
    ):
        from config.settings import settings as _s
        self._threshold   = min_confluence_score if min_confluence_score is not None \
                            else float(_s.get("idss.min_confluence_score", 0.45))
        self._warmup_bars = warmup_bars

        from core.regime.regime_classifier        import RegimeClassifier
        from core.signals.signal_generator        import SignalGenerator
        from core.meta_decision.confluence_scorer import ConfluenceScorer
        from core.meta_decision.position_sizer    import PositionSizer
        from core.risk.risk_gate                  import RiskGate

        self._regime_clf = RegimeClassifier()
        self._sig_gen    = SignalGenerator()
        self._scorer     = ConfluenceScorer(threshold=self._threshold)
        self._sizer      = PositionSizer()
        self._risk_gate  = RiskGate(
            max_concurrent_positions   = int(_s.get("risk.max_concurrent_positions", 3)),
            max_portfolio_drawdown_pct = float(_s.get("risk.max_portfolio_drawdown_pct", 15.0)),
            max_spread_pct             = float(_s.get("risk.max_spread_pct", 0.3)),
            min_risk_reward            = float(_s.get("risk.min_risk_reward", 1.0)),
        )

    def run(
        self,
        df:              pd.DataFrame,
        symbol:          str,
        timeframe:       str,
        initial_capital: float = 10_000.0,
        fee_pct:         float = 0.10,
        slippage_pct:    float = 0.05,
        spread_pct:      float = 0.05,
        progress_cb:     Optional[Callable[[str], None]] = None,
    ) -> dict:
        """Bar-by-bar backtest with enhanced trade records."""
        if df is None or df.empty:
            return self._empty_result(symbol, timeframe, initial_capital)

        n      = len(df)
        fee    = fee_pct    / 100.0
        slip   = slippage_pct / 100.0
        spread = spread_pct   / 100.0

        equity         = initial_capital
        position       = None
        equity_curve   = [initial_capital]
        chart_equity   = [initial_capital]
        eq_timestamps  = [df.index[0]]
        trades: list[dict] = []

        _report_every = max(1, n // 20)

        for i in range(1, n):
            row = df.iloc[i]
            ts  = df.index[i]

            if progress_cb and i % _report_every == 0:
                pct = int(i / n * 100)
                progress_cb(
                    f"  {symbol} [{timeframe}] bar {i:,}/{n:,} ({pct}%)"
                )

            # ── Check SL / TP ──────────────────────────────────
            if position is not None:
                direction   = position["direction"]
                triggered   = False
                exit_reason = "end_of_data"
                exit_px     = float(row["close"])

                if direction == "long":
                    if float(row["low"]) <= position["sl"]:
                        exit_px, exit_reason = position["sl"], "stop_loss"
                        triggered = True
                    elif float(row["high"]) >= position["tp"]:
                        exit_px, exit_reason = position["tp"], "take_profit"
                        triggered = True
                else:
                    if float(row["high"]) >= position["sl"]:
                        exit_px, exit_reason = position["sl"], "stop_loss"
                        triggered = True
                    elif float(row["low"]) <= position["tp"]:
                        exit_px, exit_reason = position["tp"], "take_profit"
                        triggered = True

                if triggered:
                    exit_fill = (
                        exit_px * (1.0 - slip - spread / 2) if direction == "long"
                        else exit_px * (1.0 + slip + spread / 2)
                    )
                    qty      = position["quantity"]
                    exit_fee = exit_fill * qty * fee

                    if direction == "long":
                        pnl = (exit_fill - position["entry_price"]) * qty \
                              - exit_fee - position["entry_fee"]
                    else:
                        pnl = (position["entry_price"] - exit_fill) * qty \
                              - exit_fee - position["entry_fee"]

                    pnl_pct  = pnl / (position["entry_price"] * qty) * 100.0
                    equity  += pnl

                    # ── Compute R-multiple ──────────────────────
                    entry_px   = position["entry_price"]
                    sl_px      = position["sl"]
                    size_usdt  = position["size_usdt"]
                    risk_usdt  = abs(entry_px - sl_px) / entry_px * size_usdt
                    real_r     = round(pnl / risk_usdt, 4) if risk_usdt > 0 else 0.0

                    # Expected RR: |tp - entry| / |sl - entry|
                    tp_px      = position["tp"]
                    sl_dist    = abs(entry_px - sl_px)
                    exp_rr     = round(abs(tp_px - entry_px) / sl_dist, 2) if sl_dist > 0 else 0.0

                    # Trade duration in hours
                    try:
                        entry_dt = pd.Timestamp(position["entry_time"])
                        exit_dt  = pd.Timestamp(ts)
                        dur_h    = (exit_dt - entry_dt).total_seconds() / 3600.0
                    except Exception:
                        dur_h = 0.0

                    # True regime (if available from synthetic data)
                    true_regime = str(row.get("true_regime", "")) if "true_regime" in df.columns else ""

                    trades.append({
                        # ── Standard fields ──────────────────────
                        "symbol":          symbol,
                        "entry_time":      str(position["entry_time"]),
                        "exit_time":       str(ts),
                        "entry_price":     round(entry_px, 8),
                        "exit_price":      round(exit_fill, 8),
                        "stop_price":      round(sl_px, 8),
                        "tp_price":        round(tp_px, 8),
                        "quantity":        round(qty, 8),
                        "size_usdt":       round(size_usdt, 4),
                        "direction":       direction,
                        "pnl":             round(pnl, 4),
                        "pnl_usdt":        round(pnl, 4),
                        "pnl_pct":         round(pnl_pct, 2),
                        "exit_reason":     exit_reason,
                        "duration_bars":   i - position["entry_i"],
                        "duration_hours":  round(dur_h, 2),
                        # ── Regime fields ─────────────────────────
                        "regime":          position.get("regime", "") or true_regime,
                        "regime_at_entry": position.get("regime", "") or true_regime,
                        # ── Signal fields ─────────────────────────
                        "models_fired":    position.get("models_fired", []),
                        "models_triggered": position.get("models_fired", []),
                        "confluence_score": position.get("score", 0.0),
                        "score":           position.get("score", 0.0),
                        # ── R-multiple fields ─────────────────────
                        "realized_r_multiple": real_r,
                        "expected_rr":         exp_rr,
                        "slippage_estimate":   round(slippage_pct, 4),
                        "slippage_pct":        round(slippage_pct, 4),
                        # ── Walk-forward window (set externally) ──
                        "wf_window":       position.get("wf_window", -1),
                    })
                    equity_curve.append(round(equity, 4))
                    position = None

            # ── Run IDSS pipeline if not in position ──────────
            if position is None and i >= self._warmup_bars:
                candidate = self._run_pipeline(df.iloc[: i + 1], symbol, timeframe)
                drawdown_pct = max(0.0, (1.0 - equity / initial_capital) * 100.0)
                if candidate is not None and self._passes_risk(candidate, equity, drawdown_pct):
                    direction = "long" if candidate.side == "buy" else "short"
                    entry_fill = (
                        float(row["close"]) * (1.0 + slip + spread / 2)
                        if direction == "long"
                        else float(row["close"]) * (1.0 - slip - spread / 2)
                    )
                    atr_val    = candidate.atr_value or (entry_fill * 0.008)
                    trade_value = self._sizer.calculate(
                        available_capital_usdt=equity,
                        atr_value=atr_val,
                        entry_price=entry_fill,
                        score=candidate.score,
                        regime=candidate.regime or "uncertain",
                        drawdown_pct=drawdown_pct,
                    )
                    if trade_value <= 0:
                        continue
                    qty       = trade_value / entry_fill
                    entry_fee = trade_value * fee

                    position = {
                        "entry_i":     i,
                        "entry_time":  ts,
                        "entry_price": entry_fill,
                        "quantity":    qty,
                        "entry_fee":   entry_fee,
                        "direction":   direction,
                        "sl":          candidate.stop_loss_price,
                        "tp":          candidate.take_profit_price,
                        "size_usdt":   trade_value,
                        "regime":      candidate.regime,
                        "models_fired": list(candidate.models_fired),
                        "score":       candidate.score,
                        "wf_window":   -1,  # overridden by validator
                    }
                    equity -= entry_fee

            # ── Mark-to-market for equity chart ───────────────
            if position is not None:
                curr_close = float(row["close"])
                mtm = (
                    equity + (curr_close - position["entry_price"]) * position["quantity"]
                    if position["direction"] == "long"
                    else equity + (position["entry_price"] - curr_close) * position["quantity"]
                )
            else:
                mtm = equity
            chart_equity.append(round(mtm, 4))
            eq_timestamps.append(ts)

        # ── Force-close any open position at end ──────────────
        if position is not None:
            last     = df.iloc[-1]
            close_px = float(last["close"])
            direction = position["direction"]
            exit_fill = (
                close_px * (1.0 - slip - spread / 2) if direction == "long"
                else close_px * (1.0 + slip + spread / 2)
            )
            qty      = position["quantity"]
            exit_fee = exit_fill * qty * fee
            pnl      = (
                (exit_fill - position["entry_price"]) * qty - exit_fee - position["entry_fee"]
                if direction == "long"
                else (position["entry_price"] - exit_fill) * qty - exit_fee - position["entry_fee"]
            )
            pnl_pct  = pnl / (position["entry_price"] * qty) * 100.0
            equity  += pnl

            entry_px  = position["entry_price"]
            sl_px     = position["sl"]
            tp_px     = position["tp"]
            size_usdt = position["size_usdt"]
            risk_usdt = abs(entry_px - sl_px) / entry_px * size_usdt
            real_r    = round(pnl / risk_usdt, 4) if risk_usdt > 0 else 0.0
            sl_dist   = abs(entry_px - sl_px)
            exp_rr    = round(abs(tp_px - entry_px) / sl_dist, 2) if sl_dist > 0 else 0.0

            trades.append({
                "symbol":              symbol,
                "entry_time":          str(position["entry_time"]),
                "exit_time":           str(df.index[-1]),
                "entry_price":         round(entry_px, 8),
                "exit_price":          round(exit_fill, 8),
                "stop_price":          round(sl_px, 8),
                "tp_price":            round(tp_px, 8),
                "quantity":            round(qty, 8),
                "size_usdt":           round(size_usdt, 4),
                "direction":           direction,
                "pnl":                 round(pnl, 4),
                "pnl_usdt":            round(pnl, 4),
                "pnl_pct":             round(pnl_pct, 2),
                "exit_reason":         "end_of_data",
                "duration_bars":       n - 1 - position["entry_i"],
                "duration_hours":      0.0,
                "regime":              position.get("regime", ""),
                "regime_at_entry":     position.get("regime", ""),
                "models_fired":        position.get("models_fired", []),
                "models_triggered":    position.get("models_fired", []),
                "confluence_score":    position.get("score", 0.0),
                "score":               position.get("score", 0.0),
                "realized_r_multiple": real_r,
                "expected_rr":         exp_rr,
                "slippage_estimate":   round(slippage_pct, 4),
                "slippage_pct":        round(slippage_pct, 4),
                "wf_window":           position.get("wf_window", -1),
            })
            equity_curve.append(round(equity, 4))

        from core.backtesting.idss_backtester import _calc_metrics
        metrics = _calc_metrics(trades, equity_curve, initial_capital)
        return {
            "trades":            trades,
            "equity_curve":      equity_curve,
            "chart_equity":      chart_equity,
            "equity_timestamps": [str(t) for t in eq_timestamps],
            "metrics":           metrics,
            "symbol":            symbol,
            "timeframe":         timeframe,
            "initial_capital":   initial_capital,
        }

    def _run_pipeline(self, df_window: pd.DataFrame, symbol: str, timeframe: str):
        try:
            regime, _conf, _meta = self._regime_clf.classify(df_window)
            signals = self._sig_gen.generate(symbol, df_window, regime, timeframe)
            if not signals:
                return None
            return self._scorer.score(signals, symbol)
        except Exception as exc:
            logger.debug("EnhancedIDSSBacktester pipeline error: %s", exc)
            return None

    def _passes_risk(self, candidate, equity: float, drawdown_pct: float = 0.0) -> bool:
        if candidate is None or equity <= 0:
            return False
        ep = candidate.entry_price or 0.0
        sl = candidate.stop_loss_price
        tp = candidate.take_profit_price
        if ep <= 0 or sl <= 0 or tp <= 0:
            return False
        if candidate.side == "buy"  and not (sl < ep < tp): return False
        if candidate.side == "sell" and not (tp < ep < sl): return False
        try:
            approved, _ = self._risk_gate.validate_batch(
                [candidate], open_positions=[], available_capital=equity,
                drawdown_pct=drawdown_pct, spread_map={},
            )
            return bool(approved)
        except Exception:
            return True

    @staticmethod
    def _empty_result(symbol: str, timeframe: str, initial_capital: float) -> dict:
        from core.backtesting.idss_backtester import _calc_metrics
        return {
            "trades": [], "equity_curve": [], "chart_equity": [],
            "equity_timestamps": [], "metrics": _calc_metrics([], [], initial_capital),
            "symbol": symbol, "timeframe": timeframe, "initial_capital": initial_capital,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Walk-Forward Regime-Segmented Validator
# ─────────────────────────────────────────────────────────────────────────────

class RegimeSegmentedWalkForwardValidator:
    """
    Orchestrates walk-forward regime-segmented validation across
    multiple symbols.

    For each symbol:
    1. Splits OHLCV data into sequential calibration/test windows.
    2. Runs EnhancedIDSSBacktester on each test window (using only
       data available at the start of that window).
    3. Tags each trade with the walk-forward window index.
    4. Aggregates all out-of-sample trades.

    Then across all symbols and all windows:
    5. Computes global metrics and regime-segmented metrics.
    6. Computes rolling series for stability analysis.
    7. Runs edge persistence analysis.
    """

    def __init__(self, config: Optional[WalkForwardConfig] = None):
        self._cfg = config or WalkForwardConfig()

    def run(
        self,
        ohlcv_data: Optional[dict[str, pd.DataFrame]] = None,
        progress_cb: Optional[Callable[[str], None]] = None,
    ) -> WalkForwardResult:
        """
        Run the walk-forward validation.

        Parameters
        ----------
        ohlcv_data : dict {symbol: DataFrame} or None
            Pre-fetched OHLCV DataFrames with indicators already computed.
            If None, synthetic data is generated automatically.
        progress_cb : callable
            Called with status strings during the run.
        """
        cfg = self._cfg
        result = WalkForwardResult(config=cfg)

        def log(msg: str):
            logger.info(msg)
            if progress_cb:
                progress_cb(msg)

        log("=" * 60)
        log("NexusTrader Walk-Forward Regime-Segmented Validation")
        log("=" * 60)

        # ── Step 1: Get / generate OHLCV data ─────────────────
        if ohlcv_data is None:
            log("Generating synthetic regime-labeled OHLCV data…")
            gen = SyntheticRegimeDataGenerator(seed=42)
            raw_data = gen.generate_all_symbols(cfg.symbols, cfg.timeframe)
            ohlcv_map = {}
            for sym, (df_raw, _regime_periods) in raw_data.items():
                ohlcv_map[sym] = _apply_indicators(df_raw)
            log(f"  Generated {sum(len(v) for v in ohlcv_map.values())} total bars "
                f"across {len(ohlcv_map)} symbols")
        else:
            ohlcv_map = ohlcv_data
            log(f"Using provided OHLCV data for {len(ohlcv_map)} symbols")

        result.total_symbols = len(ohlcv_map)

        # ── Step 2: Walk-forward per symbol ───────────────────
        all_trades:    list[dict] = []
        all_windows:   list[dict] = []
        equity = cfg.initial_capital
        equity_curve  = [equity]
        equity_ts     = []

        for sym_idx, (symbol, df) in enumerate(ohlcv_map.items()):
            log(f"\n[{sym_idx + 1}/{len(ohlcv_map)}] {symbol} — "
                f"{len(df)} bars total")

            sym_trades, sym_windows, equity, window_equity_ts = \
                self._run_symbol_walk_forward(
                    df, symbol, equity, log
                )

            all_trades.extend(sym_trades)
            all_windows.extend(sym_windows)
            equity_curve.extend(window_equity_ts)

            result.symbol_results[symbol] = {
                "trades":   sym_trades,
                "windows":  sym_windows,
                "n_trades": len(sym_trades),
            }

        result.windows     = all_windows
        result.window_count = len(set(w.get("window_global") for w in all_windows))
        result.all_trades  = all_trades

        log(f"\n{'─'*60}")
        log(f"Total out-of-sample trades: {len(all_trades)}")
        log("Computing analytics…")

        # ── Step 3: Sort trades chronologically ───────────────
        all_trades.sort(key=lambda t: t.get("entry_time", ""))

        # ── Step 4: Global metrics ─────────────────────────────
        result.global_metrics   = compute_metrics(all_trades)

        # ── Step 5: Dimension breakdowns ──────────────────────
        result.by_regime        = segment_metrics(all_trades, "regime_at_entry")
        result.by_asset         = segment_metrics(all_trades, "symbol")
        result.by_model         = segment_by_model(all_trades)
        result.by_score_bucket  = segment_by_score_bucket(all_trades)

        # ── Step 6: Rolling series ─────────────────────────────
        r_seq = [t.get("realized_r_multiple", 0.0) for t in all_trades
                 if t.get("realized_r_multiple") is not None]
        result.cumulative_r_history   = _cumulative_r(r_seq)
        result.rolling_20_exp_history = _rolling_exp(r_seq, 20)
        result.rolling_20_pf_history  = _rolling_pf(r_seq, 20)
        result.rolling_dd_r_history   = _rolling_dd_r(r_seq, 20)

        # ── Step 7: Equity curve ──────────────────────────────
        result.equity_curve = equity_curve

        # ── Step 8: Edge persistence verdict ──────────────────
        verdict, explanation = assess_edge_persistence(result)
        result.edge_verdict     = verdict
        result.edge_explanation = explanation

        result.generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        log(f"\nVerdict: {verdict}")
        log(explanation[:200])
        log("=" * 60)

        return result

    # ── Private helpers ───────────────────────────────────────────────────────

    def _run_symbol_walk_forward(
        self,
        df: pd.DataFrame,
        symbol: str,
        start_equity: float,
        log: Callable,
    ) -> tuple[list[dict], list[dict], float, list[float]]:
        """
        Run expanding walk-forward on a single symbol.

        Returns
        -------
        trades       : list of enhanced trade dicts
        windows      : list of window metadata dicts
        final_equity : float
        equity_ts    : list of equity values (appended per window)
        """
        cfg = self._cfg
        all_trades: list[dict]  = []
        windows:    list[dict]  = []
        equity      = start_equity
        equity_ts:  list[float] = []

        n           = len(df)
        cal_bars    = cfg.calibration_bars
        test_bars   = cfg.test_bars
        step_bars   = cfg.step_bars

        if n < cal_bars + test_bars:
            log(f"  {symbol}: insufficient data ({n} bars, need {cal_bars + test_bars})")
            return [], [], equity, equity_ts

        window_idx  = 0
        train_end   = cal_bars

        while train_end + test_bars <= n:
            test_end   = train_end + test_bars
            df_test    = df.iloc[train_end: test_end].copy()

            # Prepend calibration data for indicator warm-up.
            # The backtester uses df_test starting from bar warmup_bars (100),
            # so we prepend calibration bars to supply that history without
            # generating trades in the calibration window.
            cal_start  = max(0, train_end - cal_bars)
            df_full    = df.iloc[cal_start: test_end].copy()
            cal_len    = train_end - cal_start  # actual prepended bars

            log(f"  Window {window_idx + 1}: "
                f"calibration {df.index[cal_start].date()} → {df.index[train_end - 1].date()} "
                f"| test {df_test.index[0].date()} → {df_test.index[-1].date()}")

            bt = EnhancedIDSSBacktester(
                min_confluence_score=cfg.min_confluence_score,
                warmup_bars=cal_len,  # skip calibration bars — no trades generated there
            )
            bt_result = bt.run(
                df_full, symbol, cfg.timeframe,
                initial_capital=equity,
                fee_pct=cfg.fee_pct,
                slippage_pct=cfg.slippage_pct,
                spread_pct=cfg.spread_pct,
            )

            # Tag each trade with window index
            for t in bt_result["trades"]:
                t["wf_window"]    = window_idx
                t["symbol"]       = symbol  # ensure symbol is set

            all_trades.extend(bt_result["trades"])
            equity_ts.extend(bt_result["equity_curve"][1:])

            if bt_result["equity_curve"]:
                equity = bt_result["equity_curve"][-1]

            windows.append({
                "window":        window_idx,
                "window_global": f"{symbol}|{window_idx}",
                "symbol":        symbol,
                "cal_start":     str(df.index[cal_start].date()),
                "cal_end":       str(df.index[train_end - 1].date()),
                "test_start":    str(df_test.index[0].date()),
                "test_end":      str(df_test.index[-1].date()),
                "n_trades":      len(bt_result["trades"]),
                "metrics":       bt_result["metrics"],
                "start_equity":  start_equity,
                "end_equity":    equity,
            })

            train_end  += step_bars
            window_idx += 1

        return all_trades, windows, equity, equity_ts


# ─────────────────────────────────────────────────────────────────────────────
# Analytics helpers
# ─────────────────────────────────────────────────────────────────────────────

def _r_value(trade: dict) -> Optional[float]:
    """Extract realized R multiple from a trade dict."""
    r = trade.get("realized_r_multiple")
    if r is not None:
        return float(r)
    # Fallback: compute from entry/stop/pnl
    entry = float(trade.get("entry_price", 0) or 0)
    stop  = float(trade.get("stop_price",  0) or 0)
    size  = float(trade.get("size_usdt",   0) or 0)
    pnl   = float(trade.get("pnl",         0) or 0)
    if entry > 0 and stop > 0 and size > 0:
        risk = abs(entry - stop) / entry * size
        if risk > 0:
            return round(pnl / risk, 4)
    return None


def compute_metrics(trades: list[dict]) -> dict:
    """Compute comprehensive metrics including R-based statistics."""
    if not trades:
        return _empty_metrics()

    n      = len(trades)
    wins   = [t for t in trades if t.get("pnl", 0) > 0]
    losses = [t for t in trades if t.get("pnl", 0) <= 0]
    wr     = len(wins) / n if n else 0.0

    gross_win  = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    pf = round(gross_win / gross_loss, 4) if gross_loss > 0 else 999.0
    total_pnl  = sum(t.get("pnl", 0) for t in trades)

    # R-based metrics
    r_vals      = [_r_value(t) for t in trades]
    r_vals_clean = [r for r in r_vals if r is not None]
    r_wins  = [r for r in r_vals_clean if r > 0]
    r_loss  = [r for r in r_vals_clean if r <= 0]
    avg_win_r  = statistics.mean(r_wins)  if r_wins  else 0.0
    avg_loss_r = statistics.mean([abs(r) for r in r_loss]) if r_loss else 0.0
    expectancy_r = wr * avg_win_r - (1 - wr) * avg_loss_r if r_vals_clean else 0.0

    # Drawdown in R
    dd_r = _compute_drawdown_r(r_vals_clean)

    # Duration
    durations = [t.get("duration_hours", 0) for t in trades]
    avg_dur_h = statistics.mean(durations) if durations else 0.0

    # Win rate by exit reason
    tp_count = sum(1 for t in trades if t.get("exit_reason") == "take_profit")
    sl_count = sum(1 for t in trades if t.get("exit_reason") == "stop_loss")

    # Score coverage
    scores = [t.get("confluence_score", 0) for t in trades]
    avg_score = statistics.mean(scores) if scores else 0.0

    return {
        "total_trades":    n,
        "win_rate":        round(wr * 100, 1),
        "win_rate_frac":   round(wr, 4),
        "loss_rate":       round((1 - wr) * 100, 1),
        "total_pnl_usdt":  round(total_pnl, 2),
        "gross_win_usdt":  round(gross_win, 2),
        "gross_loss_usdt": round(gross_loss, 2),
        "profit_factor":   min(round(pf, 4), 999.0),
        "avg_win_r":       round(avg_win_r, 4),
        "avg_loss_r":      round(avg_loss_r, 4),
        "expectancy_r":    round(expectancy_r, 4),
        "drawdown_r":      round(dd_r, 4),
        "avg_duration_h":  round(avg_dur_h, 2),
        "tp_count":        tp_count,
        "sl_count":        sl_count,
        "avg_score":       round(avg_score, 4),
    }


def _empty_metrics() -> dict:
    return {
        "total_trades": 0, "win_rate": 0.0, "win_rate_frac": 0.0,
        "loss_rate": 0.0, "total_pnl_usdt": 0.0, "gross_win_usdt": 0.0,
        "gross_loss_usdt": 0.0, "profit_factor": 0.0,
        "avg_win_r": 0.0, "avg_loss_r": 0.0, "expectancy_r": 0.0,
        "drawdown_r": 0.0, "avg_duration_h": 0.0, "tp_count": 0,
        "sl_count": 0, "avg_score": 0.0,
    }


def segment_metrics(trades: list[dict], key: str) -> dict[str, dict]:
    """Segment metrics by a trade dict key."""
    groups: dict[str, list[dict]] = {}
    for t in trades:
        val = str(t.get(key, "unknown") or "unknown").lower()
        groups.setdefault(val, []).append(t)
    return {k: compute_metrics(v) for k, v in sorted(groups.items())}


def segment_by_model(trades: list[dict]) -> dict[str, dict]:
    """Segment metrics by IDSS model (trades may have multiple models)."""
    groups: dict[str, list[dict]] = {}
    for t in trades:
        for m in (t.get("models_fired") or t.get("models_triggered") or []):
            groups.setdefault(str(m), []).append(t)
    return {k: compute_metrics(v) for k, v in sorted(groups.items())}


def segment_by_score_bucket(trades: list[dict]) -> dict[str, dict]:
    """Segment metrics by confluence score bucket."""
    bins = [(0.30, 0.45), (0.45, 0.55), (0.55, 0.65), (0.65, 0.75),
            (0.75, 0.85), (0.85, 1.01)]
    groups: dict[str, list[dict]] = {}
    for t in trades:
        sc = float(t.get("confluence_score", 0) or t.get("score", 0) or 0)
        for lo, hi in bins:
            if lo <= sc < hi:
                lbl = f"{lo:.2f}-{hi:.2f}".replace("1.01", "1.00")
                groups.setdefault(lbl, []).append(t)
                break
    return {k: compute_metrics(v) for k, v in sorted(groups.items())}


def _cumulative_r(r_seq: list[float]) -> list[float]:
    cum, total = [], 0.0
    for r in r_seq:
        total += r
        cum.append(round(total, 4))
    return cum


def _rolling_exp(r_seq: list[float], window: int) -> list[float]:
    result = []
    for i in range(window - 1, len(r_seq)):
        chunk = r_seq[i - window + 1: i + 1]
        wins  = [r for r in chunk if r > 0]
        loss  = [r for r in chunk if r <= 0]
        wr    = len(wins) / len(chunk)
        aw    = statistics.mean(wins)  if wins else 0.0
        al    = statistics.mean([abs(r) for r in loss]) if loss else 0.0
        result.append(round(wr * aw - (1 - wr) * al, 4))
    return result


def _rolling_pf(r_seq: list[float], window: int) -> list[float]:
    result = []
    for i in range(window - 1, len(r_seq)):
        chunk = r_seq[i - window + 1: i + 1]
        gw = sum(r for r in chunk if r > 0)
        gl = abs(sum(r for r in chunk if r <= 0))
        result.append(round(gw / gl, 4) if gl > 0 else 999.0)
    return result


def _rolling_dd_r(r_seq: list[float], window: int) -> list[float]:
    """Rolling max drawdown in R over a trailing window."""
    result = []
    for i in range(len(r_seq)):
        start = max(0, i - window + 1)
        chunk = r_seq[start: i + 1]
        result.append(round(_compute_drawdown_r(chunk), 4))
    return result


def _compute_drawdown_r(r_seq: list[float]) -> float:
    if not r_seq:
        return 0.0
    cum, total, peak, max_dd = [], 0.0, None, 0.0
    for r in r_seq:
        total += r
        cum.append(total)
    peak = cum[0]
    for v in cum:
        if v > peak:
            peak = v
        dd = peak - v
        if dd > max_dd:
            max_dd = dd
    return round(max_dd, 4)


def assess_edge_persistence(result: WalkForwardResult) -> tuple[str, str]:
    """
    Assess whether NexusTrader demonstrates persistent edge.

    Returns (verdict, explanation).
    Verdict: "PERSISTENT_EDGE" | "REGIME_DEPENDENT" | "INSUFFICIENT_DATA"
    """
    trades = result.all_trades
    gm     = result.global_metrics

    if len(trades) < 20:
        return (
            "INSUFFICIENT_DATA",
            f"Only {len(trades)} out-of-sample trades generated.  "
            "Need ≥ 20 trades for reliable edge assessment.  "
            "Consider extending the data period or lowering the confluence threshold."
        )

    # ── Global edge checks ─────────────────────────────────────
    global_exp  = gm.get("expectancy_r", 0.0)
    global_pf   = gm.get("profit_factor", 0.0)
    global_wr   = gm.get("win_rate_frac", 0.0)
    global_dd_r = gm.get("drawdown_r", 0.0)

    # ── Regime coverage ────────────────────────────────────────
    regimes_with_trades = {k: v for k, v in result.by_regime.items()
                           if v.get("total_trades", 0) >= 3}
    regimes_positive = {k: v for k, v in regimes_with_trades.items()
                        if v.get("expectancy_r", 0) > 0}
    regime_coverage = len(regimes_positive) / max(len(regimes_with_trades), 1)

    # ── Asset concentration check ──────────────────────────────
    asset_counts = {k: v.get("total_trades", 0) for k, v in result.by_asset.items()}
    max_asset_pct = max(asset_counts.values()) / max(sum(asset_counts.values()), 1) if asset_counts else 0

    # ── Model concentration check ─────────────────────────────
    model_pnls = {k: v.get("total_pnl_usdt", 0) for k, v in result.by_model.items()}
    total_model_pnl = sum(v for v in model_pnls.values() if v > 0)
    max_model_pct   = (max(model_pnls.values(), default=0) / max(total_model_pnl, 1e-9)
                       if total_model_pnl > 0 else 0)

    # ── PFS rolling stability ─────────────────────────────────
    pf_hist = result.rolling_20_pf_history
    if len(pf_hist) >= 5:
        snap   = pf_hist[-10:] if len(pf_hist) >= 10 else pf_hist
        finite = [min(v, 5.0) for v in snap]
        mean_pf = statistics.mean(finite)
        std_pf  = statistics.stdev(finite) if len(finite) >= 2 else 0.0
        pfs_cv  = std_pf / mean_pf if mean_pf > 0 else 1.0
        pfs_score = max(0, min(100, round(100 * (1 - pfs_cv))))
        pfs_label = "Stable" if pfs_score >= 85 else (
            "Moderate" if pfs_score >= 60 else "Unstable"
        )
    else:
        pfs_score, pfs_label = 0, "Insufficient data"

    # ── Window-by-window consistency ──────────────────────────
    window_exp_list = []
    for win in result.windows:
        wt = win.get("n_trades", 0)
        wm = win.get("metrics", {})
        if wt >= 3:
            # Compute expectancy for this window's trades
            w_trades = [t for t in trades if
                        t.get("symbol") == win.get("symbol") and
                        t.get("wf_window") == win.get("window")]
            wm2 = compute_metrics(w_trades)
            window_exp_list.append(wm2.get("expectancy_r", 0.0))

    windows_positive = sum(1 for e in window_exp_list if e > 0)
    window_consistency = (windows_positive / len(window_exp_list)
                         if window_exp_list else 0.0)

    # ── Verdict ───────────────────────────────────────────────
    strengths  = []
    weaknesses = []

    if global_exp > 0.10:
        strengths.append(f"strong overall expectancy (+{global_exp:.3f}R)")
    elif global_exp > 0:
        strengths.append(f"positive overall expectancy (+{global_exp:.3f}R)")
    else:
        weaknesses.append(f"negative overall expectancy ({global_exp:.3f}R)")

    if global_pf >= 1.40:
        strengths.append(f"strong PF ({global_pf:.2f})")
    elif global_pf >= 1.20:
        strengths.append(f"adequate PF ({global_pf:.2f})")
    else:
        weaknesses.append(f"low PF ({global_pf:.2f}, target ≥ 1.40)")

    if regime_coverage >= 0.75:
        strengths.append(
            f"positive edge in {len(regimes_positive)}/{len(regimes_with_trades)} regimes tested"
        )
    else:
        weaknesses.append(
            f"regime-dependent: positive in only {len(regimes_positive)}/{len(regimes_with_trades)} regimes"
        )

    if max_asset_pct <= 0.5:
        strengths.append("diversified across assets (no single asset > 50%)")
    else:
        best_asset = max(asset_counts, key=asset_counts.get, default="unknown")
        weaknesses.append(
            f"concentrated: {best_asset} drives {max_asset_pct*100:.0f}% of trades"
        )

    if pfs_label in ("Stable", "Moderate"):
        strengths.append(f"PF stability: {pfs_label} ({pfs_score}/100)")
    else:
        weaknesses.append(f"PF stability: {pfs_label} ({pfs_score}/100)")

    if window_consistency >= 0.60:
        strengths.append(
            f"consistent across walk-forward windows ({windows_positive}/{len(window_exp_list)} positive)"
        )
    else:
        weaknesses.append(
            f"inconsistent windows ({windows_positive}/{len(window_exp_list)} positive)"
        )

    if global_dd_r < 5.0:
        strengths.append(f"controlled R-drawdown ({global_dd_r:.2f}R)")
    elif global_dd_r < 10.0:
        weaknesses.append(f"moderate R-drawdown ({global_dd_r:.2f}R)")
    else:
        weaknesses.append(f"high R-drawdown ({global_dd_r:.2f}R, limit 10R)")

    # ── Final verdict ─────────────────────────────────────────
    is_persistent = (
        global_exp > 0
        and global_pf >= 1.10
        and regime_coverage >= 0.50
        and global_dd_r < 10.0
        and (window_consistency >= 0.50 or len(window_exp_list) < 3)
    )

    verdict = "PERSISTENT_EDGE" if is_persistent else "REGIME_DEPENDENT"

    parts = []
    if strengths:
        parts.append("STRENGTHS: " + "; ".join(strengths) + ".")
    if weaknesses:
        parts.append("WEAKNESSES: " + "; ".join(weaknesses) + ".")

    if verdict == "PERSISTENT_EDGE":
        conclusion = (
            "CONCLUSION: Strategy demonstrates persistent edge across regimes "
            "and appears suitable for demo trading."
        )
    else:
        conclusion = (
            "CONCLUSION: Strategy appears regime-dependent or unstable and "
            "requires further observation before scaling live capital."
        )

    explanation = "  ".join(parts + [conclusion])
    return verdict, explanation


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _apply_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Apply calculate_all() to a DataFrame, returning the enriched copy."""
    from core.features.indicator_library import calculate_all
    return calculate_all(df)
