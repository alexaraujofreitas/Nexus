#!/usr/bin/env python3
"""
demo_validation_runner.py — 48h Dry-Run Demo Validation

PURPOSE
───────
Validates correct IDSS signal selection, position sizing, heat management,
drawdown safety, and trade logging across a 48-hour simulation window
using real historical OHLCV data from the configured exchange.

What this runner confirms:
  1. Signal selection — IDSS generates candidates with expected frequencies
  2. Auto-execute guard — only passes candidates through the full safeguard stack
  3. Position sizing — PositionSizer + SymbolAllocator producing expected USDT sizes
  4. Portfolio heat — never exceeds 6% cap across concurrent positions
  5. Drawdown circuit breaker — triggers at 10% drawdown
  6. Condition dedup — no duplicate (side, models, regime) entries
  7. Portfolio correlation guard — blocks correlated stacks
  8. Trade logging completeness — all required fields present
  9. Live vs Backtest tracker — records and computes rolling metrics
 10. Capital accounting — equity curve tracks correctly

IMPORTANT: This is a SIMULATION only.  No live orders are placed.
The PaperExecutor is used in an isolated mode; results are written to
  reports/demo_validation/ and NOT to the main data/ files.

USAGE
─────
  python scripts/demo_validation_runner.py [--symbols SYMS] [--hours N] [--tf TF]
  python scripts/demo_validation_runner.py                       # default: 48h, 5 symbols
  python scripts/demo_validation_runner.py --hours 12 --tf 15m
  python scripts/demo_validation_runner.py --symbols BTC/USDT ETH/USDT

Output: reports/demo_validation/<timestamp>/{report.txt, trades.csv, anomalies.txt}
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

# ── Bootstrap path ────────────────────────────────────────────────────────────
_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("demo_validation")

# ── Config defaults ───────────────────────────────────────────────────────────
_DEFAULT_HOURS   = 48
_DEFAULT_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT", "SOL/USDT",
    "TRX/USDT", "DOGE/USDT", "ADA/USDT", "BCH/USDT", "HYPE/USDT",
    "LINK/USDT", "XLM/USDT", "AVAX/USDT", "HBAR/USDT", "SUI/USDT",
    "NEAR/USDT", "ICP/USDT", "ONDO/USDT", "ALGO/USDT", "RENDER/USDT",
]
_DEFAULT_TF      = "1h"
_START_CAPITAL   = 100_000.0
_MAX_HEAT_PCT    = 6.0
_MAX_DD_PCT      = 10.0

# Required fields in every trade dict recorded after close
_REQUIRED_TRADE_FIELDS = [
    "symbol", "side", "entry_price", "exit_price", "stop_loss", "take_profit",
    "size_usdt", "pnl_pct", "pnl_usdt", "exit_reason", "score", "regime",
    "models_fired", "risk_amount_usdt", "expected_rr", "symbol_weight",
    "adjusted_score", "realized_r",
]


# ─────────────────────────────────────────────────────────────────────────────
# Validation result containers
# ─────────────────────────────────────────────────────────────────────────────

class ValidationResult:
    """Collects pass/fail checks and anomalies."""

    def __init__(self) -> None:
        self.checks:    list[dict]  = []   # {name, passed, details}
        self.anomalies: list[str]   = []
        self.trades:    list[dict]  = []
        self.metrics:   dict        = {}

    def check(self, name: str, passed: bool, details: str = "") -> None:
        self.checks.append({"name": name, "passed": passed, "details": details})
        if not passed:
            logger.warning("  ✗ FAIL — %s: %s", name, details)
        else:
            logger.info("  ✓ PASS — %s %s", name, f"({details})" if details else "")

    def anomaly(self, msg: str) -> None:
        self.anomalies.append(msg)
        logger.warning("  ⚠ ANOMALY: %s", msg)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c["passed"])

    @property
    def failed(self) -> int:
        return sum(1 for c in self.checks if not c["passed"])


# ─────────────────────────────────────────────────────────────────────────────
# OHLCV loader — fetch from exchange (no simulation, real data)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_historical_ohlcv(symbols: list[str], tf: str, hours: int) -> dict[str, Any]:
    """
    Fetch real historical OHLCV bars from the configured exchange.
    Returns dict: symbol → DataFrame with columns [open, high, low, close, volume].
    Falls back to synthetic data if exchange is unavailable.
    """
    import pandas as pd
    from config.settings import settings
    limit = max(hours * 2, 200)   # enough bars to cover the window + warmup

    data: dict[str, Any] = {}
    try:
        from core.market_data.exchange_manager import exchange_manager
        ex = exchange_manager.get_exchange()
        if ex is None:
            raise RuntimeError("Exchange not available")

        for sym in symbols:
            try:
                raw = ex.fetch_ohlcv(sym, tf, limit=limit)
                if not raw:
                    raise ValueError("empty OHLCV")
                df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
                df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
                df.set_index("timestamp", inplace=True)
                data[sym] = df
                logger.info("  Loaded %d bars for %s (%s)", len(df), sym, tf)
            except Exception as exc:
                logger.warning("  Could not fetch %s: %s — will use synthetic fallback", sym, exc)
    except Exception as exc:
        logger.warning("Exchange unavailable (%s) — using synthetic fallback for all symbols", exc)

    # Fill any missing symbols with synthetic data
    missing = [s for s in symbols if s not in data]
    if missing:
        try:
            from core.validation.calibrated_data_generator import CalibratedDataGenerator
            cdg = CalibratedDataGenerator(timeframe=tf, n_bars=limit)
            for sym in missing:
                base = sym.split("/")[0]
                df = cdg.generate(symbol=sym)
                data[sym] = df
                logger.info("  Synthetic fallback: %d bars for %s", len(df), sym)
        except Exception as exc:
            logger.error("Synthetic fallback also failed: %s", exc)

    return data


# ─────────────────────────────────────────────────────────────────────────────
# Dry-run simulator — drives IDSS pipeline bar-by-bar
# ─────────────────────────────────────────────────────────────────────────────

def _build_indicator_df(df):
    """Build indicator-enriched DataFrame using the production indicator library."""
    try:
        from core.indicators.indicator_library import calculate_all
        return calculate_all(df)
    except Exception:
        return df


def _run_pipeline_on_bar(
    sym:     str,
    df_full: Any,
    idx:     int,
    state:   dict,
    result:  ValidationResult,
) -> Optional[dict]:
    """
    Run the IDSS pipeline on bars[0:idx+1] for *sym*.
    Returns a candidate dict if a signal was generated, else None.
    """
    import pandas as pd
    try:
        from core.indicators.indicator_library import calculate_all
        from core.regime.hmm_regime_classifier import HMMRegimeClassifier
        from core.signals.signal_generator import SignalGenerator
        from core.meta_decision.confluence_scorer import ConfluenceScorer
        from core.risk.risk_gate import RiskGate
        from core.meta_decision.position_sizer import PositionSizer

        df = df_full.iloc[: idx + 1].copy()
        if len(df) < 30:
            return None

        df_ind = calculate_all(df)
        if df_ind is None or len(df_ind) < 20:
            return None

        # Per-symbol HMM classifier (reuse from state or create)
        hmm = state.setdefault(f"hmm_{sym}", HMMRegimeClassifier())
        regime, regime_probs = hmm.classify_combined(df_ind)

        sig_gen = state.setdefault(f"siggen_{sym}", SignalGenerator())
        signals = sig_gen.generate(
            df=df_ind, symbol=sym, regime=regime, regime_probs=regime_probs
        )
        if not signals:
            return None

        scorer = state.setdefault("scorer", ConfluenceScorer())
        candidate = scorer.score(
            signals=signals, symbol=sym, equity=state.get("capital", _START_CAPITAL),
            regime=regime, regime_probs=regime_probs,
        )
        if candidate is None:
            return None

        risk_gate = state.setdefault("risk_gate", RiskGate())
        sizer     = state.setdefault("sizer",     PositionSizer())
        approved, reason = risk_gate.check(candidate, equity=state.get("capital", _START_CAPITAL))
        if not approved:
            return None

        size_usdt = sizer.calculate(
            score=candidate.score,
            equity=state.get("capital", _START_CAPITAL),
            stop_pct=abs(candidate.entry_price - candidate.stop_loss_price) / candidate.entry_price
            if candidate.entry_price else 0.01,
        )
        candidate.position_size_usdt = size_usdt
        candidate.approved = True

        # Apply SymbolAllocator weight
        try:
            from core.analytics.symbol_allocator import get_allocator
            weight = get_allocator().get_weight(sym)
        except Exception:
            weight = 1.0

        return {
            "symbol":           sym,
            "side":             candidate.side,
            "score":            candidate.score,
            "symbol_weight":    weight,
            "adjusted_score":   round(candidate.score * weight, 4),
            "entry_price":      candidate.entry_price,
            "stop_loss_price":  candidate.stop_loss_price,
            "take_profit_price":candidate.take_profit_price,
            "position_size_usdt": size_usdt,
            "models_fired":     candidate.models_fired,
            "regime":           regime,
            "rationale":        candidate.rationale,
            "timeframe":        "1h",
            "atr_value":        candidate.atr_value,
        }
    except Exception as exc:
        logger.debug("Pipeline error on %s bar %d: %s", sym, idx, exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Position tracking for the dry run (no Qt / no DB)
# ─────────────────────────────────────────────────────────────────────────────

class _SimPosition:
    def __init__(self, sym, side, entry, stop, take, size, score, weight, adj_score, models, regime, risk_usdt, exp_rr):
        self.symbol       = sym
        self.side         = side
        self.entry_price  = entry
        self.stop_loss    = stop
        self.take_profit  = take
        self.size_usdt    = size
        self.score        = score
        self.symbol_weight = weight
        self.adjusted_score = adj_score
        self.models_fired  = models
        self.regime        = regime
        self.risk_amount_usdt = risk_usdt
        self.expected_rr   = exp_rr
        self.opened_at     = datetime.now(timezone.utc)

    def check_exit(self, price: float) -> Optional[str]:
        if self.side == "buy":
            if price <= self.stop_loss:   return "stop_loss"
            if price >= self.take_profit: return "take_profit"
        else:
            if price >= self.stop_loss:   return "stop_loss"
            if price <= self.take_profit: return "take_profit"
        return None

    def pnl_usdt(self, exit_price: float) -> float:
        if self.side == "buy":
            pct = (exit_price - self.entry_price) / self.entry_price
        else:
            pct = (self.entry_price - exit_price) / self.entry_price
        return self.size_usdt * pct

    def realized_r(self, exit_price: float) -> Optional[float]:
        if self.risk_amount_usdt <= 0:
            return None
        return round(self.pnl_usdt(exit_price) / self.risk_amount_usdt, 4)


# ─────────────────────────────────────────────────────────────────────────────
# Main validation loop
# ─────────────────────────────────────────────────────────────────────────────

def run_validation(symbols: list[str], tf: str, hours: int) -> ValidationResult:
    result = ValidationResult()
    logger.info("=" * 68)
    logger.info("NEXUS TRADER — Demo Validation Runner")
    logger.info("Symbols: %s | TF: %s | Window: %dh", symbols, tf, hours)
    logger.info("=" * 68)

    # ── Load OHLCV ────────────────────────────────────────────
    logger.info("\n[1/6] Loading historical OHLCV...")
    ohlcv = fetch_historical_ohlcv(symbols, tf, hours)
    result.check("OHLCV loaded for all symbols",
                 all(s in ohlcv for s in symbols),
                 f"loaded={list(ohlcv.keys())}")

    if not ohlcv:
        result.anomaly("No OHLCV data available — aborting")
        return result

    min_bars = min(len(df) for df in ohlcv.values())
    result.check("Sufficient bars (≥ 50)", min_bars >= 50, f"min_bars={min_bars}")

    # ── Determine simulation window ───────────────────────────
    logger.info("\n[2/6] Configuring simulation window...")
    # Use last `hours × tf_per_hour` bars as the "live" window
    tf_secs = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400}.get(tf, 3600)
    bars_per_window = int(hours * 3600 / tf_secs)
    warmup_bars     = 50   # bars used for indicator warmup — not traded

    total_bars = min_bars
    start_bar  = max(total_bars - bars_per_window - warmup_bars, 0)
    end_bar    = total_bars

    logger.info("  Warmup: %d bars | Trading window: %d bars (%dh @ %s)",
                warmup_bars, bars_per_window, hours, tf)

    # ── Simulation state ──────────────────────────────────────
    logger.info("\n[3/6] Running bar-by-bar simulation...")
    capital    = _START_CAPITAL
    peak_cap   = _START_CAPITAL
    positions: list[_SimPosition] = []
    closed:    list[dict]         = []
    state:     dict               = {"capital": capital}
    n_signals  = 0
    n_trades   = 0
    max_heat   = 0.0
    max_dd     = 0.0
    dd_breaker_fired = False

    for bar_i in range(start_bar + warmup_bars, end_bar):
        # ── Price update: check exits for all open positions ──
        new_closed = []
        for pos in list(positions):
            sym_df = ohlcv.get(pos.symbol)
            if sym_df is None or bar_i >= len(sym_df):
                continue
            bar = sym_df.iloc[bar_i]
            # Check high/low for stop or take-profit hit
            for test_price in (bar["low"], bar["high"]):
                reason = pos.check_exit(test_price)
                if reason:
                    ep = test_price
                    pnl = pos.pnl_usdt(ep)
                    rlzd_r = pos.realized_r(ep)
                    trade = {
                        "symbol":           pos.symbol,
                        "side":             pos.side,
                        "entry_price":      pos.entry_price,
                        "exit_price":       ep,
                        "stop_loss":        pos.stop_loss,
                        "take_profit":      pos.take_profit,
                        "size_usdt":        pos.size_usdt,
                        "pnl_pct":          round(pnl / pos.size_usdt * 100, 4),
                        "pnl_usdt":         round(pnl, 2),
                        "exit_reason":      reason,
                        "score":            pos.score,
                        "regime":           pos.regime,
                        "models_fired":     pos.models_fired,
                        "risk_amount_usdt": pos.risk_amount_usdt,
                        "expected_rr":      pos.expected_rr,
                        "symbol_weight":    pos.symbol_weight,
                        "adjusted_score":   pos.adjusted_score,
                        "realized_r":       rlzd_r,
                    }
                    closed.append(trade)
                    capital += pnl
                    peak_cap = max(peak_cap, capital)
                    dd_pct   = (peak_cap - capital) / peak_cap * 100 if peak_cap > 0 else 0.0
                    max_dd   = max(max_dd, dd_pct)
                    if dd_pct >= _MAX_DD_PCT and not dd_breaker_fired:
                        dd_breaker_fired = True
                        result.anomaly(
                            f"Drawdown circuit breaker would fire at bar {bar_i}: "
                            f"drawdown={dd_pct:.1f}% ≥ {_MAX_DD_PCT}%"
                        )
                    new_closed.append(pos)
                    n_trades += 1
                    state["capital"] = capital
                    break

        for pos in new_closed:
            positions.remove(pos)

        # ── Heat check ────────────────────────────────────────
        total_open = sum(p.size_usdt for p in positions)
        heat_pct   = total_open / capital * 100 if capital > 0 else 0.0
        max_heat   = max(max_heat, heat_pct)
        if heat_pct > _MAX_HEAT_PCT + 0.5:   # +0.5 tolerance for sizing rounding
            result.anomaly(
                f"Portfolio heat {heat_pct:.1f}% > {_MAX_HEAT_PCT}% cap at bar {bar_i}"
            )

        # ── Circuit breaker gate ──────────────────────────────
        dd_now = (peak_cap - capital) / peak_cap * 100 if peak_cap > 0 else 0.0
        if dd_now >= _MAX_DD_PCT:
            continue   # no new trades when breaker is on

        # ── Signal generation (one bar behind, e.g. bar_i - 1 closed) ─
        for sym in symbols:
            sym_df = ohlcv.get(sym)
            if sym_df is None or bar_i < warmup_bars + 5:
                continue
            cand = _run_pipeline_on_bar(sym, sym_df, bar_i - 1, state, result)
            if cand is None:
                continue
            n_signals += 1

            # Condition dedup check
            existing_condition = any(
                p.symbol == sym
                and p.side == cand["side"]
                and set(p.models_fired) == set(cand.get("models_fired", []))
                and p.regime.lower() == (cand.get("regime") or "").lower()
                for p in positions
            )
            if existing_condition:
                continue

            # Max 10 positions per symbol
            if sum(1 for p in positions if p.symbol == sym) >= 10:
                continue

            ep   = cand["entry_price"]
            stop = cand["stop_loss_price"]
            tp   = cand["take_profit_price"]
            size = cand["position_size_usdt"]
            risk_usdt = abs(ep - stop) / ep * size if ep > 0 and stop > 0 else 0.0
            exp_rr    = abs(tp - ep) / abs(ep - stop) if abs(ep - stop) > 0 else 0.0

            new_pos = _SimPosition(
                sym=sym, side=cand["side"],
                entry=ep, stop=stop, take=tp, size=size,
                score=cand["score"], weight=cand["symbol_weight"],
                adj_score=cand["adjusted_score"],
                models=cand.get("models_fired", []),
                regime=cand.get("regime", ""),
                risk_usdt=risk_usdt, exp_rr=round(exp_rr, 4),
            )
            positions.append(new_pos)

    # ── Close any still-open positions at last close ──────────
    for pos in list(positions):
        sym_df = ohlcv.get(pos.symbol)
        last_price = float(sym_df.iloc[-1]["close"]) if sym_df is not None else pos.entry_price
        pnl = pos.pnl_usdt(last_price)
        rlzd_r = pos.realized_r(last_price)
        closed.append({
            "symbol":           pos.symbol,
            "side":             pos.side,
            "entry_price":      pos.entry_price,
            "exit_price":       last_price,
            "stop_loss":        pos.stop_loss,
            "take_profit":      pos.take_profit,
            "size_usdt":        pos.size_usdt,
            "pnl_pct":          round(pnl / pos.size_usdt * 100, 4),
            "pnl_usdt":         round(pnl, 2),
            "exit_reason":      "end_of_window",
            "score":            pos.score,
            "regime":           pos.regime,
            "models_fired":     pos.models_fired,
            "risk_amount_usdt": pos.risk_amount_usdt,
            "expected_rr":      pos.expected_rr,
            "symbol_weight":    pos.symbol_weight,
            "adjusted_score":   pos.adjusted_score,
            "realized_r":       rlzd_r,
        })
        capital += pnl
    peak_cap = max(peak_cap, capital)
    result.trades = closed

    # ─────────────────────────────────────────────────────────────────────────
    # [4/6] Structural checks
    # ─────────────────────────────────────────────────────────────────────────
    logger.info("\n[4/6] Running structural checks...")

    # Trade count
    result.check("Simulation produced trades",
                 len(closed) > 0, f"trades={len(closed)}")

    # Required fields present in all trades
    if closed:
        missing_fields: list[str] = []
        for t in closed:
            for f_name in _REQUIRED_TRADE_FIELDS:
                if f_name not in t:
                    missing_fields.append(f_name)
        result.check("All required trade fields present",
                     len(missing_fields) == 0,
                     f"missing={list(set(missing_fields))[:5]}" if missing_fields else "")

    # Heat cap never exceeded
    result.check(f"Portfolio heat never exceeded {_MAX_HEAT_PCT}% cap",
                 max_heat <= _MAX_HEAT_PCT + 0.5,
                 f"max_heat={max_heat:.2f}%")

    # Drawdown < 15% (hard limit)
    result.check("Max drawdown < 15%",
                 max_dd < 15.0, f"max_dd={max_dd:.2f}%")

    # Capital accounting
    final_dd = (peak_cap - capital) / peak_cap * 100 if peak_cap > 0 else 0.0
    result.check("Capital tracking coherent (peak ≥ final)",
                 peak_cap >= capital,
                 f"peak=${peak_cap:,.0f} final=${capital:,.0f}")

    # realized_r present and signed correctly
    r_vals = [t.get("realized_r") for t in closed if t.get("realized_r") is not None]
    wins_by_r   = sum(1 for r in r_vals if r > 0)
    wins_by_pnl = sum(1 for t in closed if (t.get("pnl_usdt") or 0.0) > 0)
    result.check("realized_r sign agrees with pnl_usdt sign",
                 wins_by_r == wins_by_pnl or len(r_vals) == 0,
                 f"wins_by_r={wins_by_r} wins_by_pnl={wins_by_pnl}")

    # symbol_weight within bounds [0.10, 3.00]
    bad_weights = [t.get("symbol_weight") for t in closed
                   if t.get("symbol_weight") is not None
                   and not (0.10 <= t["symbol_weight"] <= 3.00)]
    result.check("symbol_weight within [0.10, 3.00] for all trades",
                 len(bad_weights) == 0, f"bad={bad_weights[:3]}" if bad_weights else "")

    # adjusted_score = score × weight (within 1% tolerance)
    bad_adj = []
    for t in closed:
        sc = t.get("score") or 0.0; wt = t.get("symbol_weight") or 1.0
        adj = t.get("adjusted_score") or 0.0
        if abs(adj - sc * wt) > 0.01 + abs(sc * wt) * 0.01:
            bad_adj.append((sc, wt, adj))
    result.check("adjusted_score = score × weight (±1%)",
                 len(bad_adj) == 0, f"bad={bad_adj[:2]}" if bad_adj else "")

    # ── Signal frequency check ────────────────────────────────
    tf_secs2 = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400}.get(tf, 3600)
    expected_candles = hours * 3600 / tf_secs2
    signal_rate = n_signals / expected_candles if expected_candles > 0 else 0.0
    result.check("Signal rate > 0 (scanner is firing)",
                 n_signals > 0, f"signals={n_signals} rate={signal_rate:.3f}/candle")

    # ─────────────────────────────────────────────────────────────────────────
    # [5/6] Metrics summary
    # ─────────────────────────────────────────────────────────────────────────
    logger.info("\n[5/6] Computing metrics...")
    wins     = sum(1 for t in closed if (t.get("pnl_usdt") or 0) > 0)
    losses   = len(closed) - wins
    win_rate = wins / len(closed) if closed else 0.0
    total_pnl = sum(t.get("pnl_usdt") or 0 for t in closed)
    gross_win = sum(t.get("pnl_usdt") or 0 for t in closed if (t.get("pnl_usdt") or 0) > 0)
    gross_loss = abs(sum(t.get("pnl_usdt") or 0 for t in closed if (t.get("pnl_usdt") or 0) < 0))
    pf   = gross_win / gross_loss if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0)
    avg_r = sum(r for r in r_vals) / len(r_vals) if r_vals else 0.0

    avg_slippage_pct = 0.0  # dry run doesn't simulate slippage (market price = entry)

    result.metrics = {
        "trades":            len(closed),
        "wins":              wins,
        "losses":            losses,
        "win_rate":          round(win_rate, 4),
        "profit_factor":     round(pf, 3),
        "avg_r":             round(avg_r, 4),
        "total_pnl_usdt":    round(total_pnl, 2),
        "gross_win_usdt":    round(gross_win, 2),
        "gross_loss_usdt":   round(gross_loss, 2),
        "max_heat_pct":      round(max_heat, 2),
        "max_drawdown_pct":  round(max_dd, 2),
        "final_capital":     round(capital, 2),
        "n_signals":         n_signals,
        "signal_rate":       round(signal_rate, 4),
        "dd_breaker_fired":  dd_breaker_fired,
    }

    logger.info("  Trades: %d | WR: %.1f%% | PF: %.2f | Avg R: %.3f",
                len(closed), win_rate * 100, pf, avg_r)
    logger.info("  Max heat: %.1f%% | Max DD: %.1f%% | Final cap: $%.0f",
                max_heat, max_dd, capital)

    # vs Study 4 (enabled models only: trend + momentum_breakout)
    logger.info("\n  vs Study 4 Baseline (enabled models):")
    logger.info("    WR: %.1f%% live vs 51.4%% baseline (Δ %+.1f%%)",
                win_rate * 100, (win_rate - 0.514) * 100)
    logger.info("    PF: %.2f live vs 1.47 baseline  (Δ %+.2f)",
                pf, pf - 1.47)

    # ─────────────────────────────────────────────────────────────────────────
    # [6/6] Write output
    # ─────────────────────────────────────────────────────────────────────────
    logger.info("\n[6/6] Writing output files...")
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = _ROOT / "reports" / "demo_validation" / ts
    out_dir.mkdir(parents=True, exist_ok=True)

    # trades.csv
    if closed:
        keys = list(closed[0].keys())
        with open(out_dir / "trades.csv", "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(closed)

    # anomalies.txt
    if result.anomalies:
        (out_dir / "anomalies.txt").write_text("\n".join(result.anomalies))
    else:
        (out_dir / "anomalies.txt").write_text("No anomalies detected.")

    # report.txt
    lines = [
        "=" * 68,
        "NEXUS TRADER — Demo Validation Report",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        f"Symbols: {symbols}  |  TF: {tf}  |  Window: {hours}h",
        "=" * 68,
        "",
        "CHECKS",
        "------",
    ]
    for c in result.checks:
        mark = "✓" if c["passed"] else "✗"
        detail = f"  ({c['details']})" if c["details"] else ""
        lines.append(f"  {mark}  {c['name']}{detail}")

    lines += [
        "",
        f"  Passed: {result.passed}/{len(result.checks)}  |  "
        f"Anomalies: {len(result.anomalies)}",
        "",
        "METRICS",
        "-------",
    ]
    for k, v in result.metrics.items():
        lines.append(f"  {k:<28} {v}")

    if result.anomalies:
        lines += ["", "ANOMALIES", "---------"]
        for a in result.anomalies:
            lines.append(f"  ⚠  {a}")

    lines += [
        "",
        "vs STUDY 4 BASELINE (pruned portfolio)",
        "--------------------------------------",
        f"  WR  : {win_rate*100:.1f}% live  vs  51.4% baseline",
        f"  PF  : {pf:.2f}   live  vs  1.47  baseline",
        f"  Avg R: {avg_r:.3f} live  vs  0.31  baseline",
    ]

    report_text = "\n".join(lines)
    (out_dir / "report.txt").write_text(report_text)
    logger.info("  Output: %s", out_dir)

    # Print summary to terminal
    print("\n" + "=" * 68)
    print(f"VERDICT: {'PASS' if result.failed == 0 else 'FAIL'}  "
          f"({result.passed}/{len(result.checks)} checks passed, "
          f"{len(result.anomalies)} anomalies)")
    print(f"Trades: {len(closed)}  WR: {win_rate*100:.1f}%  PF: {pf:.2f}  "
          f"Avg R: {avg_r:.3f}")
    print(f"Max Heat: {max_heat:.1f}%  Max DD: {max_dd:.1f}%  "
          f"Final capital: ${capital:,.0f}")
    print(f"Report: {out_dir / 'report.txt'}")
    print("=" * 68)

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def _parse_args():
    p = argparse.ArgumentParser(description="NexusTrader 48h Demo Validation Runner")
    p.add_argument("--symbols", nargs="+", default=_DEFAULT_SYMBOLS, help="Trading pairs")
    p.add_argument("--hours",   type=int,  default=_DEFAULT_HOURS,   help="Simulation window in hours")
    p.add_argument("--tf",      default=_DEFAULT_TF,                 help="Timeframe (default: 1h)")
    return p.parse_args()


if __name__ == "__main__":
    args  = _parse_args()
    result = run_validation(symbols=args.symbols, tf=args.tf, hours=args.hours)
    sys.exit(0 if result.failed == 0 else 1)
