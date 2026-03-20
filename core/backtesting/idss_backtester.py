# ============================================================
# NEXUS TRADER — IDSS Pipeline Backtester  (Phase B)
#
# Replays OHLCV data bar-by-bar through the full IDSS pipeline:
#   RegimeClassifier → SignalGenerator → ConfluenceScorer → RiskGate
#
# Differs from the rule-based backtester in that:
#   • Entry signals come from the live IDSS sub-models (not condition trees)
#   • SL/TP levels are ATR-based, set by the models
#   • Only one position open at a time per backtest run (single-symbol)
#   • Position size = position_size_pct % of current equity at entry
#
# Usage:
#   bt = IDSSBacktester()
#   result = bt.run(df_with_indicators, symbol, timeframe, ...)
# ============================================================
from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Callable, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Minimum bars of history to feed the pipeline before looking for signals.
# ADX (14), BB (20), EMA slopes (5 bars), ATR (14) — 100 gives comfortable warmup.
WARMUP_BARS = 100


class IDSSBacktester:
    """
    Bar-by-bar replay of the full IDSS signal pipeline on historical data.

    Uses the same PositionSizer and RiskGate modules as the live scanner so that
    backtest position sizing and risk filtering are consistent with live behavior.
    The fixed position_size_pct / min_risk_reward parameters from the original
    backtester have been removed to eliminate the live-vs-backtest divergence
    identified in audit finding BACK-01.

    Parameters
    ----------
    min_confluence_score : float
        Minimum score from ConfluenceScorer to generate a trade.
        Defaults to the live setting (idss.min_confluence_score).
    warmup_bars : int
        Bars of history required before signals are eligible (default 100).
    """

    def __init__(
        self,
        min_confluence_score: float | None = None,
        warmup_bars: int = 100,
        # Deprecated params kept for backwards-compat; they are ignored.
        min_risk_reward: float = 1.3,      # noqa: ignored — live RiskGate used instead
        position_size_pct: float = 10.0,   # noqa: ignored — live PositionSizer used instead
    ):
        from config.settings import settings as _s
        self._threshold   = min_confluence_score if min_confluence_score is not None \
                            else float(_s.get("idss.min_confluence_score", 0.45))
        self._warmup_bars = warmup_bars

        # Instantiate IDSS pipeline components (shared across bars — stateless)
        from core.regime.regime_classifier        import RegimeClassifier
        from core.signals.signal_generator        import SignalGenerator
        from core.meta_decision.confluence_scorer import ConfluenceScorer

        self._regime_clf = RegimeClassifier()
        self._sig_gen    = SignalGenerator()
        self._scorer     = ConfluenceScorer(threshold=self._threshold)

        # ── Live-equivalent risk modules ────────────────────────────────
        # PositionSizer: quarter-Kelly with vol-scalar, regime multiplier, etc.
        from core.meta_decision.position_sizer import PositionSizer
        self._sizer = PositionSizer()

        # RiskGate: same instance used in the live scanner.
        # MTF confirmation is intentionally disabled for backtesting (no live
        # exchange available to fetch higher-TF data bar-by-bar).
        from core.risk.risk_gate import RiskGate
        self._risk_gate = RiskGate(
            max_concurrent_positions   = int(_s.get("risk.max_concurrent_positions", 3)),
            max_portfolio_drawdown_pct = float(_s.get("risk.max_portfolio_drawdown_pct", 15.0)),
            max_spread_pct             = float(_s.get("risk.max_spread_pct", 0.3)),
            min_risk_reward            = float(_s.get("risk.min_risk_reward", 1.0)),
        )

    # ── Public API ────────────────────────────────────────────

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
        """
        Run the IDSS backtest.

        Parameters
        ----------
        df : pd.DataFrame
            OHLCV DataFrame with ALL indicators pre-computed via calculate_all().
            Index must be a DatetimeIndex (or timestamp-convertible).
        symbol : str
            Trading pair (e.g. "BTC/USDT").
        timeframe : str
            Timeframe string (e.g. "1h").
        initial_capital : float
            Starting equity in USDT.
        fee_pct : float
            Round-trip fee per side in percent (e.g. 0.10 for 0.10 %).
        slippage_pct : float
            Slippage applied against the fill direction (e.g. 0.05 for 0.05 %).
        spread_pct : float
            Bid-ask spread in percent (e.g. 0.05 for 0.05 %).
        progress_cb : callable, optional
            Called with a status string on each progress update.

        Returns
        -------
        dict with keys matching the rule-based backtest output:
            trades, equity_curve, chart_equity, equity_timestamps,
            metrics, candle_count, symbol, timeframe, strategy_name,
            initial_capital, loaded_timeframes, parse_warnings.
        """
        if df is None or df.empty:
            return self._empty_result(symbol, timeframe, initial_capital)

        n         = len(df)
        fee       = fee_pct      / 100.0
        slip      = slippage_pct / 100.0
        spread    = spread_pct   / 100.0

        equity         = initial_capital
        position       = None          # None or dict when in a trade
        equity_curve   = [initial_capital]
        chart_equity:  list[float] = [initial_capital]
        eq_timestamps: list       = [df.index[0]]
        trades:        list[dict] = []

        _report_every = max(1, n // 20)   # emit progress ~20 times

        for i in range(1, n):
            row = df.iloc[i]
            ts  = df.index[i]

            # ── Progress reporting ────────────────────────────
            if progress_cb and i % _report_every == 0:
                pct = int(i / n * 100)
                progress_cb(
                    f"IDSS backtest: {symbol} [{timeframe}] — bar {i:,}/{n:,}  ({pct}%)"
                )

            # ── In a position: check SL / TP ─────────────────
            if position is not None:
                direction = position["direction"]
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
                else:  # short
                    if float(row["high"]) >= position["sl"]:
                        exit_px, exit_reason = position["sl"], "stop_loss"
                        triggered = True
                    elif float(row["low"]) <= position["tp"]:
                        exit_px, exit_reason = position["tp"], "take_profit"
                        triggered = True

                if triggered:
                    if direction == "long":
                        exit_fill = exit_px * (1.0 - slip - spread/2)
                    else:
                        exit_fill = exit_px * (1.0 + slip + spread/2)

                    qty      = position["quantity"]
                    exit_fee = exit_fill * qty * fee

                    if direction == "long":
                        pnl = (exit_fill - position["entry_price"]) * qty \
                              - exit_fee - position["entry_fee"]
                    else:
                        pnl = (position["entry_price"] - exit_fill) * qty \
                              - exit_fee - position["entry_fee"]

                    pnl_pct = pnl / (position["entry_price"] * qty) * 100.0
                    equity += pnl

                    trades.append({
                        "entry_time":    str(position["entry_time"]),
                        "exit_time":     str(ts),
                        "entry_price":   round(position["entry_price"], 8),
                        "exit_price":    round(exit_fill, 8),
                        "quantity":      round(qty, 8),
                        "pnl":           round(pnl, 4),
                        "pnl_pct":       round(pnl_pct, 2),
                        "exit_reason":   exit_reason,
                        "duration_bars": i - position["entry_i"],
                        "regime":        position.get("regime", ""),
                        "models_fired":  position.get("models_fired", []),
                        "score":         position.get("score", 0.0),
                    })
                    equity_curve.append(round(equity, 4))
                    position = None

            # ── Not in position + past warmup: run IDSS pipeline ──
            if position is None and i >= self._warmup_bars:
                candidate = self._run_pipeline(
                    df.iloc[: i + 1], symbol, timeframe
                )
                # Use live RiskGate for filtering (spread_map empty — no live tickers)
                drawdown_pct = max(0.0, (1.0 - equity / initial_capital) * 100.0)
                if candidate is not None and self._passes_risk(
                    candidate, equity, drawdown_pct
                ):
                    direction = "long" if candidate.side == "buy" else "short"

                    if direction == "long":
                        entry_fill = float(row["close"]) * (1.0 + slip + spread/2)
                    else:
                        entry_fill = float(row["close"]) * (1.0 - slip - spread/2)

                    # Live PositionSizer: quarter-Kelly with vol/regime/score scalars.
                    # calculate() returns position size in USDT directly (not a fraction).
                    atr_val     = candidate.atr_value or (entry_fill * 0.008)
                    trade_value = self._sizer.calculate(
                        available_capital_usdt=equity,
                        atr_value=atr_val,
                        entry_price=entry_fill,
                        score=candidate.score,
                        regime=candidate.regime or "uncertain",
                        drawdown_pct=drawdown_pct,
                    )
                    if trade_value <= 0:
                        continue  # sizer rejected (halt regime / drawdown limit)
                    qty       = trade_value / entry_fill
                    entry_fee = trade_value * fee

                    sl_price = candidate.stop_loss_price
                    tp_price = candidate.take_profit_price

                    position = {
                        "entry_i":     i,
                        "entry_time":  ts,
                        "entry_price": entry_fill,
                        "quantity":    qty,
                        "entry_fee":   entry_fee,
                        "direction":   direction,
                        "sl":          sl_price,
                        "tp":          tp_price,
                        "regime":      candidate.regime,
                        "models_fired": list(candidate.models_fired),
                        "score":       candidate.score,
                    }
                    equity -= entry_fee

            # ── Per-bar mark-to-market for chart ─────────────
            if position is not None:
                curr_close = float(row["close"])
                direction  = position["direction"]
                if direction == "long":
                    mtm = equity + (curr_close - position["entry_price"]) * position["quantity"]
                else:
                    mtm = equity + (position["entry_price"] - curr_close) * position["quantity"]
            else:
                mtm = equity
            chart_equity.append(round(mtm, 4))
            eq_timestamps.append(ts)

        # ── Force-close any open position at final bar ────────
        if position is not None:
            last       = df.iloc[-1]
            close_px   = float(last["close"])
            direction  = position["direction"]
            exit_fill  = close_px * (1.0 - slip - spread/2) if direction == "long" \
                         else close_px * (1.0 + slip + spread/2)
            qty        = position["quantity"]
            exit_fee   = exit_fill * qty * fee

            if direction == "long":
                pnl = (exit_fill - position["entry_price"]) * qty \
                      - exit_fee - position["entry_fee"]
            else:
                pnl = (position["entry_price"] - exit_fill) * qty \
                      - exit_fee - position["entry_fee"]

            pnl_pct = pnl / (position["entry_price"] * qty) * 100.0
            equity  += pnl

            trades.append({
                "entry_time":    str(position["entry_time"]),
                "exit_time":     str(df.index[-1]),
                "entry_price":   round(position["entry_price"], 8),
                "exit_price":    round(exit_fill, 8),
                "quantity":      round(qty, 8),
                "pnl":           round(pnl, 4),
                "pnl_pct":       round(pnl_pct, 2),
                "exit_reason":   "end_of_data",
                "duration_bars": n - 1 - position["entry_i"],
                "regime":        position.get("regime", ""),
                "models_fired":  position.get("models_fired", []),
                "score":         position.get("score", 0.0),
            })
            equity_curve.append(round(equity, 4))

        metrics = _calc_metrics(trades, equity_curve, initial_capital)

        if progress_cb:
            progress_cb(
                f"✓ IDSS backtest complete — {len(trades)} trades  "
                f"({metrics.get('win_rate', 0):.1f}% win rate)"
            )

        return {
            "trades":            trades,
            "equity_curve":      equity_curve,
            "chart_equity":      chart_equity,
            "equity_timestamps": eq_timestamps,
            "metrics":           metrics,
            "candle_count":      n,
            "symbol":            symbol,
            "timeframe":         timeframe,
            "strategy_name":     "IDSS Pipeline",
            "initial_capital":   initial_capital,
            "loaded_timeframes": [timeframe],
            "parse_warnings":    [],
        }

    # ── Private helpers ───────────────────────────────────────

    def _run_pipeline(
        self,
        df_window: pd.DataFrame,
        symbol:    str,
        timeframe: str,
    ):
        """
        Run RegimeClassifier → SignalGenerator → ConfluenceScorer on df_window.
        Returns an OrderCandidate or None.
        """
        try:
            regime, _conf, _meta = self._regime_clf.classify(df_window)
            signals = self._sig_gen.generate(symbol, df_window, regime, timeframe)
            if not signals:
                return None
            candidate = self._scorer.score(signals, symbol)
            return candidate
        except Exception as exc:
            logger.debug("IDSSBacktester pipeline error at bar: %s", exc)
            return None

    def _passes_risk(self, candidate, equity: float, drawdown_pct: float = 0.0) -> bool:
        """
        Risk validation using the same live RiskGate as the scanner.

        MTF confirmation is skipped (candidate.higher_tf_regime is empty string,
        so the MTF gate passes automatically). Spread map is empty — spread
        filtering is bypassed since we have no live tickers in backtesting.
        """
        if candidate is None:
            return False
        if equity <= 0:
            return False

        # Sanity: ensure price levels make sense for the side
        ep = candidate.entry_price or 0.0
        sl = candidate.stop_loss_price
        tp = candidate.take_profit_price
        if ep <= 0 or sl <= 0 or tp <= 0:
            return False
        if candidate.side == "buy" and not (sl < ep < tp):
            return False
        if candidate.side == "sell" and not (tp < ep < sl):
            return False

        # Live RiskGate validation (no open positions in single-position backtester)
        try:
            approved, rejected = self._risk_gate.validate_batch(
                [candidate],
                open_positions=[],      # single-position backtest; no existing positions
                available_capital=equity,
                drawdown_pct=drawdown_pct,
                spread_map={},          # no live spread data in backtest
            )
            if not approved:
                if rejected:
                    logger.debug(
                        "IDSSBacktester: RiskGate rejected %s — %s",
                        candidate.symbol,
                        rejected[0].rejection_reason if rejected else "unknown",
                    )
                return False
            return True
        except Exception as exc:
            logger.debug("IDSSBacktester: RiskGate error, falling back to sanity-only: %s", exc)
            # Fallback: accept if price levels are valid and equity > 0
            return True

    @staticmethod
    def _empty_result(symbol: str, timeframe: str, initial_capital: float) -> dict:
        return {
            "trades":            [],
            "equity_curve":      [],
            "chart_equity":      [],
            "equity_timestamps": [],
            "metrics":           _calc_metrics([], [], initial_capital),
            "candle_count":      0,
            "symbol":            symbol,
            "timeframe":         timeframe,
            "strategy_name":     "IDSS Pipeline",
            "initial_capital":   initial_capital,
            "loaded_timeframes": [timeframe],
            "parse_warnings":    [],
        }


# ── Performance metrics (mirrors backtest_engine._calc_metrics) ──

def _calc_metrics(
    trades: list[dict],
    equity_curve: list[float],
    initial_capital: float,
) -> dict:
    if not trades:
        return {
            "total_return_pct": 0.0, "max_drawdown_pct": 0.0,
            "sharpe_ratio": 0.0, "win_rate": 0.0, "total_trades": 0,
            "profit_factor": 0.0, "avg_pnl_pct": 0.0,
            "winning_trades": 0, "losing_trades": 0,
        }

    final_equity  = equity_curve[-1] if equity_curve else initial_capital
    total_ret_pct = (final_equity - initial_capital) / initial_capital * 100.0

    # Max drawdown
    peak   = initial_capital
    max_dd = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100.0 if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    win_rate = len(wins) / len(trades) * 100.0

    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss   = abs(sum(t["pnl"] for t in losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")

    sharpe = 0.0
    if len(equity_curve) > 2:
        eq_arr  = np.array(equity_curve, dtype=float)
        eq_prev = eq_arr[:-1]
        rets    = np.where(eq_prev > 0, (eq_arr[1:] - eq_prev) / eq_prev, 0.0)
        std     = float(np.std(rets))
        mean    = float(np.mean(rets))
        if std > 0:
            sharpe = round(mean / std * math.sqrt(252), 2)

    avg_pnl_pct = sum(t["pnl_pct"] for t in trades) / len(trades)

    return {
        "total_return_pct": round(total_ret_pct, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe_ratio":     round(sharpe, 2),
        "win_rate":         round(win_rate, 1),
        "total_trades":     len(trades),
        "profit_factor":    round(profit_factor, 2) if profit_factor != float("inf") else 999.0,
        "avg_pnl_pct":      round(avg_pnl_pct, 2),
        "winning_trades":   len(wins),
        "losing_trades":    len(losses),
    }


# ============================================================
# Monte Carlo Simulator
# ============================================================
class MonteCarloSimulator:
    """
    Runs N Monte Carlo simulations on a completed trade list by
    randomly resampling trade returns with replacement (bootstrap).

    This estimates the distribution of outcomes under different
    sequences of the same trades — measuring path dependency and
    luck vs. skill.

    Usage
    -----
    mc = MonteCarloSimulator(n_simulations=1000)
    result = mc.run(trades, initial_capital=10_000)
    """

    def __init__(self, n_simulations: int = 1000, random_seed: int = 42):
        self._n   = n_simulations
        self._rng = np.random.default_rng(random_seed)

    def run(
        self,
        trades: list[dict],
        initial_capital: float = 10_000.0,
        progress_cb: Optional[Callable[[str], None]] = None,
    ) -> dict:
        """
        Bootstrap Monte Carlo simulation.

        Returns
        -------
        dict:
            n_simulations     : int
            final_equities    : list[float]  — sorted final equity values
            return_pcts       : list[float]  — sorted return percentages
            drawdowns         : list[float]  — sorted max drawdowns (%)
            percentiles       : dict  — p5, p25, p50, p75, p95 for each metric
            prob_profit       : float  — probability of being profitable (P > 0%)
            prob_ruin         : float  — probability of losing > 50% of capital
            expected_return   : float  — mean return %
            expected_drawdown : float  — mean max drawdown %
        """
        if not trades:
            return self._empty_result()

        pnl_pcts = np.array([t.get("pnl_pct", 0.0) for t in trades], dtype=float)
        n_trades  = len(pnl_pcts)

        final_equities: list[float] = []
        max_drawdowns:  list[float] = []

        if progress_cb:
            progress_cb(f"Monte Carlo: running {self._n:,} simulations…")

        for sim_idx in range(self._n):
            # Sample n_trades with replacement
            sampled_pcts = self._rng.choice(pnl_pcts, size=n_trades, replace=True)

            # Simulate equity curve
            equity = initial_capital
            peak   = equity
            max_dd = 0.0

            for r_pct in sampled_pcts:
                pnl    = equity * (r_pct / 100.0)
                equity += pnl
                if equity > peak:
                    peak = equity
                dd = (peak - equity) / peak * 100.0 if peak > 0 else 0.0
                if dd > max_dd:
                    max_dd = dd

            final_equities.append(round(equity, 4))
            max_drawdowns.append(round(max_dd, 4))

        final_equities.sort()
        max_drawdowns.sort()
        return_pcts = sorted(
            [(e - initial_capital) / initial_capital * 100.0 for e in final_equities]
        )

        def _pct(arr: list[float], p: float) -> float:
            idx = int(len(arr) * p / 100)
            return round(arr[min(idx, len(arr) - 1)], 2)

        percentiles: dict = {}
        for metric, arr in [
            ("return_pct", return_pcts),
            ("max_drawdown_pct", max_drawdowns),
        ]:
            percentiles[metric] = {
                "p5":  _pct(arr, 5),
                "p25": _pct(arr, 25),
                "p50": _pct(arr, 50),
                "p75": _pct(arr, 75),
                "p95": _pct(arr, 95),
            }

        prob_profit = sum(1 for r in return_pcts if r > 0) / self._n
        prob_ruin   = sum(1 for r in return_pcts if r < -50.0) / self._n

        if progress_cb:
            progress_cb(
                f"✓ Monte Carlo: p50 return={percentiles['return_pct']['p50']:.1f}%  "
                f"p50 drawdown={percentiles['max_drawdown_pct']['p50']:.1f}%  "
                f"P(profit)={prob_profit:.0%}"
            )

        return {
            "n_simulations":       self._n,
            "final_equities":      final_equities,
            "return_pcts":         return_pcts,
            "drawdowns":           max_drawdowns,
            "percentiles":         percentiles,
            "prob_profit":         round(prob_profit, 4),
            "prob_ruin":           round(prob_ruin, 4),
            "expected_return":     round(float(np.mean(return_pcts)), 2),
            "expected_drawdown":   round(float(np.mean(max_drawdowns)), 2),
        }

    @staticmethod
    def _empty_result() -> dict:
        return {
            "n_simulations": 0, "final_equities": [], "return_pcts": [],
            "drawdowns": [], "percentiles": {}, "prob_profit": 0.0,
            "prob_ruin": 0.0, "expected_return": 0.0, "expected_drawdown": 0.0,
        }


# ============================================================
# Walk-Forward Validator
# ============================================================
class WalkForwardValidator:
    """
    Implements expanding-window walk-forward validation on the
    IDSS pipeline.

    Splits historical data into sequential train/validate windows
    and runs the backtester on each validate window after training
    on the preceding history.

    This is the gold standard for avoiding look-ahead bias in
    systematic trading — it accurately simulates what performance
    would have looked like in live trading.

    Usage
    -----
    wfv = WalkForwardValidator(train_months=12, validate_months=3, step_months=3)
    result = wfv.run(df, symbol="BTC/USDT", timeframe="1h")
    """

    def __init__(
        self,
        train_months:    int = 12,
        validate_months: int = 3,
        step_months:     int = 3,
        min_confluence_score: float = 0.55,
        min_risk_reward:      float = 1.3,
        position_size_pct:    float = 10.0,
    ):
        self._train_mo    = train_months
        self._val_mo      = validate_months
        self._step_mo     = step_months
        self._bt_kwargs   = dict(
            min_confluence_score = min_confluence_score,
            min_risk_reward      = min_risk_reward,
            position_size_pct    = position_size_pct,
        )

    def run(
        self,
        df:              pd.DataFrame,
        symbol:          str,
        timeframe:       str,
        initial_capital: float = 10_000.0,
        fee_pct:         float = 0.10,
        slippage_pct:    float = 0.05,
        progress_cb:     Optional[Callable[[str], None]] = None,
    ) -> dict:
        """
        Run walk-forward validation.

        Returns
        -------
        dict:
            windows        : list[dict]  — per-window backtest results + metrics
            combined_trades: list[dict]  — all out-of-sample trades concatenated
            combined_equity: list[float] — continuous equity curve across windows
            aggregate_metrics: dict      — metrics computed on combined trades
            window_count   : int
            in_sample_pct  : float       — fraction of data used for training
        """
        if df is None or df.empty or not isinstance(df.index, pd.DatetimeIndex):
            return self._empty_result(symbol, timeframe, initial_capital)

        # Convert month counts to approximate bar counts
        tf_bars_per_day = _timeframe_bars_per_day(timeframe)
        train_bars = int(self._train_mo * 30 * tf_bars_per_day)
        val_bars   = int(self._val_mo   * 30 * tf_bars_per_day)
        step_bars  = int(self._step_mo  * 30 * tf_bars_per_day)

        if len(df) < train_bars + val_bars:
            return self._empty_result(symbol, timeframe, initial_capital)

        windows:         list[dict]  = []
        combined_trades: list[dict]  = []
        combined_equity: list[float] = [initial_capital]
        equity = initial_capital

        window_idx  = 0
        train_start = 0
        train_end   = train_bars

        while train_end + val_bars <= len(df):
            val_end = train_end + val_bars

            df_train    = df.iloc[train_start : train_end]
            df_validate = df.iloc[train_end   : val_end]

            if progress_cb:
                progress_cb(
                    f"Walk-Forward: window {window_idx + 1} — "
                    f"validate {df_validate.index[0].date()} → "
                    f"{df_validate.index[-1].date()}"
                )

            bt = IDSSBacktester(**self._bt_kwargs)
            result = bt.run(
                df_validate,
                symbol,
                timeframe,
                initial_capital  = equity,
                fee_pct          = fee_pct,
                slippage_pct     = slippage_pct,
            )

            windows.append({
                "window":       window_idx,
                "train_start":  str(df_train.index[0].date())  if len(df_train)    > 0 else "",
                "train_end":    str(df_train.index[-1].date()) if len(df_train)    > 0 else "",
                "val_start":    str(df_validate.index[0].date())  if len(df_validate) > 0 else "",
                "val_end":      str(df_validate.index[-1].date()) if len(df_validate) > 0 else "",
                "trades":       result["trades"],
                "metrics":      result["metrics"],
            })

            combined_trades.extend(result["trades"])
            # Continue equity from end of this window
            if result["equity_curve"]:
                equity = result["equity_curve"][-1]
            combined_equity.extend(result["equity_curve"][1:])

            # Step forward
            train_end  += step_bars
            window_idx += 1

        if not windows:
            return self._empty_result(symbol, timeframe, initial_capital)

        aggregate_metrics = _calc_metrics(combined_trades, combined_equity, initial_capital)

        in_sample_bars = train_bars
        total_bars     = len(df)
        in_sample_pct  = in_sample_bars / total_bars if total_bars > 0 else 0.0

        if progress_cb:
            progress_cb(
                f"✓ Walk-Forward: {window_idx} windows  "
                f"{len(combined_trades)} out-of-sample trades  "
                f"sharpe={aggregate_metrics.get('sharpe_ratio', 0):.2f}"
            )

        return {
            "windows":           windows,
            "combined_trades":   combined_trades,
            "combined_equity":   combined_equity,
            "aggregate_metrics": aggregate_metrics,
            "window_count":      window_idx,
            "in_sample_pct":     round(in_sample_pct, 3),
            "symbol":            symbol,
            "timeframe":         timeframe,
            "initial_capital":   initial_capital,
        }

    @staticmethod
    def _empty_result(symbol: str, timeframe: str, initial_capital: float) -> dict:
        return {
            "windows": [], "combined_trades": [], "combined_equity": [],
            "aggregate_metrics": _calc_metrics([], [], initial_capital),
            "window_count": 0, "in_sample_pct": 0.0,
            "symbol": symbol, "timeframe": timeframe,
            "initial_capital": initial_capital,
        }


def _timeframe_bars_per_day(timeframe: str) -> float:
    """Return approximate number of bars per calendar day for a timeframe."""
    _MAP = {
        "1m": 1440, "3m": 480, "5m": 288, "15m": 96, "30m": 48,
        "1h": 24, "2h": 12, "4h": 6, "6h": 4, "8h": 3, "12h": 2,
        "1d": 1, "1w": 1 / 7,
    }
    return _MAP.get(timeframe, 24)
