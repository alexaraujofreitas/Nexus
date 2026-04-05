# ============================================================
# NEXUS TRADER — Staged Backtester (Phase 5)
#
# PURPOSE
# ───────
# Extends the EnhancedIDSSBacktester with dual-timeframe
# simulation to validate the Phase 4 staged candidate
# architecture (HTF signal → LTF confirmation → execution).
#
# Two modes:
#   BASELINE  — 1H signal → immediate execution (legacy)
#   STAGED    — 1H signal → CREATED → 15m confirmation cycles
#               → CONFIRMED → execution (or VOIDED/EXPIRED)
#
# TIME SIMULATION
# ───────────────
# Each 1H bar is decomposed into 4 synthetic 15m sub-bars
# using OHLC interpolation.  The LTF confirmation evaluator
# runs on these sub-bars, simulating the real LTF scan cycle.
#
# NO ARCHITECTURE CHANGES
# ──────────────────────
# This module is STRICTLY an evaluation framework.  It does
# NOT modify any production code.
# ============================================================
from __future__ import annotations

import logging
import math
import time as _time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Candidate State Machine (backtester-internal, mirrors CandidateStore)
# ─────────────────────────────────────────────────────────────────────────────

class _CandidateState:
    CREATED   = "CREATED"
    CONFIRMED = "CONFIRMED"
    EXECUTED  = "EXECUTED"
    EXPIRED   = "EXPIRED"
    VOIDED    = "VOIDED"

_TERMINAL = {_CandidateState.EXECUTED, _CandidateState.EXPIRED, _CandidateState.VOIDED}


@dataclass
class _SimCandidate:
    """In-memory candidate for backtest simulation."""
    cid:             int
    symbol:          str
    side:            str          # "buy" or "sell"
    entry_price:     float
    stop_loss:       float
    take_profit:     float
    score:           float
    models_fired:    list
    regime:          str
    atr_value:       float
    fingerprint:     tuple

    state:           str   = _CandidateState.CREATED
    created_at_bar:  int   = 0     # 1H bar index
    created_at_sub:  int   = 0     # sub-bar index within that 1H bar
    confirmed_at_bar: int  = -1
    confirmed_at_sub: int  = -1
    executed_at_bar: int   = -1
    voided_at_bar:   int   = -1
    expired_at_bar:  int   = -1
    void_reason:     str   = ""

    # LTF data at confirmation
    ltf_rsi:         float = 0.0
    ltf_ema_slope:   float = 0.0
    ltf_volume_ratio: float = 0.0
    ltf_close:       float = 0.0

    @property
    def is_active(self) -> bool:
        return self.state not in _TERMINAL

    def confirm(self, bar: int, sub: int, **ltf_data):
        assert self.state == _CandidateState.CREATED
        self.state = _CandidateState.CONFIRMED
        self.confirmed_at_bar = bar
        self.confirmed_at_sub = sub
        for k, v in ltf_data.items():
            if hasattr(self, k):
                setattr(self, k, v)

    def execute(self, bar: int):
        assert self.state == _CandidateState.CONFIRMED
        self.state = _CandidateState.EXECUTED
        self.executed_at_bar = bar

    def void(self, bar: int, reason: str = ""):
        assert self.state == _CandidateState.CREATED
        self.state = _CandidateState.VOIDED
        self.voided_at_bar = bar
        self.void_reason = reason

    def expire(self, bar: int):
        assert self.state == _CandidateState.CREATED
        self.state = _CandidateState.EXPIRED
        self.expired_at_bar = bar


def _make_fingerprint(symbol, side, models_fired, regime):
    return (symbol, side.lower(), frozenset(models_fired or []), (regime or "").lower())


# ─────────────────────────────────────────────────────────────────────────────
# 15m Sub-Bar Interpolation
# ─────────────────────────────────────────────────────────────────────────────

def interpolate_15m_bars(row_1h: pd.Series, rng: np.random.Generator) -> list[dict]:
    """
    Decompose a single 1H OHLCV bar into 4 synthetic 15m sub-bars.

    The interpolation preserves the 1H bar's OHLC envelope:
    - Sub-bar 1 opens at the 1H open
    - Sub-bar 4 closes at the 1H close
    - All sub-bars stay within [1H low, 1H high]
    - Volume is split roughly evenly with slight randomness

    Returns list of 4 dicts with keys: open, high, low, close, volume
    """
    o = float(row_1h["open"])
    h = float(row_1h["high"])
    l = float(row_1h["low"])
    c = float(row_1h["close"])
    v = float(row_1h["volume"])

    # Generate 3 intermediate price points between open and close
    # constrained to [low, high]
    mid_range = h - l
    if mid_range <= 0:
        mid_range = abs(c) * 0.001 or 1.0

    # Create path: open → p1 → p2 → p3 → close
    # p1, p2, p3 are random walks within the 1H envelope
    points = [o]
    for _ in range(3):
        step = rng.normal(0, mid_range * 0.15)
        p = points[-1] + step
        p = max(l, min(h, p))
        points.append(p)
    points.append(c)  # 5 points = 4 intervals

    # Volume split with slight randomness
    vol_weights = rng.dirichlet([2.0, 2.0, 2.0, 2.0])
    sub_vols = v * vol_weights

    bars = []
    for j in range(4):
        sub_o = points[j]
        sub_c = points[j + 1]
        sub_h = max(sub_o, sub_c) + abs(rng.normal(0, mid_range * 0.05))
        sub_l = min(sub_o, sub_c) - abs(rng.normal(0, mid_range * 0.05))
        # Clamp to 1H envelope
        sub_h = min(sub_h, h)
        sub_l = max(sub_l, l)
        # Ensure OHLC consistency
        sub_h = max(sub_h, sub_o, sub_c)
        sub_l = min(sub_l, sub_o, sub_c)
        bars.append({
            "open": sub_o,
            "high": sub_h,
            "low": sub_l,
            "close": sub_c,
            "volume": float(sub_vols[j]),
        })
    return bars


# ─────────────────────────────────────────────────────────────────────────────
# Lightweight LTF Confirmation (backtester-internal, mirrors ltf_confirmation)
# ─────────────────────────────────────────────────────────────────────────────

def _compute_ema(closes: list[float], period: int) -> list[float]:
    """Simple EMA on a list of close prices."""
    if len(closes) < period:
        return [float("nan")] * len(closes)
    ema = [float("nan")] * (period - 1)
    sma = sum(closes[:period]) / period
    ema.append(sma)
    k = 2.0 / (period + 1)
    for i in range(period, len(closes)):
        ema.append(ema[-1] * (1 - k) + closes[i] * k)
    return ema


def _compute_rsi(closes: list[float], period: int) -> list[float]:
    """Simple RSI on a list of close prices."""
    if len(closes) < period + 1:
        return [float("nan")] * len(closes)
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(0, d) for d in deltas]
    losses = [max(0, -d) for d in deltas]

    rsi_vals = [float("nan")] * period
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period

    for i in range(period, len(deltas)):
        if avg_l == 0:
            rsi_vals.append(100.0 if avg_g > 0 else 50.0)
        else:
            rsi_vals.append(100.0 - 100.0 / (1.0 + avg_g / avg_l))
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period

    # Pad to match length of closes (deltas is len-1)
    return [float("nan")] + rsi_vals


def _evaluate_ltf_confirmation(
    sub_bar_history: list[dict],
    side: str,
    ema_period: int = 9,
    rsi_period: int = 14,
    rsi_max_long: float = 72.0,
    rsi_min_long: float = 0.0,       # Phase 6: lower RSI bound for longs
    rsi_min_short: float = 28.0,
    rsi_max_short: float = 100.0,    # Phase 6: upper RSI bound for shorts
    rsi_void_long: float = 78.0,
    rsi_void_short: float = 22.0,
    volume_ratio_min: float = 0.8,
    volume_lookback: int = 20,
    ema_slope_bars: int = 3,
    ema_slope_min: float = 0.0,      # Phase 6: minimum EMA slope magnitude (%)
) -> dict:
    """
    Evaluate LTF confirmation on accumulated 15m sub-bar history.

    Phase 6 additions:
    - ema_slope_min: minimum magnitude of EMA slope (e.g. 0.001 = 0.1%)
    - rsi_min_long: lower bound for RSI on long entries (e.g. 45 for momentum)
    - rsi_max_short: upper bound for RSI on short entries (e.g. 55 for momentum)

    Returns dict with: confirmed, voided, void_reason, rsi, ema_slope, volume_ratio, ltf_close
    """
    if len(sub_bar_history) < max(ema_period, rsi_period + 1, volume_lookback):
        return {"confirmed": False, "voided": False, "void_reason": None,
                "rsi": float("nan"), "ema_slope": 0.0, "volume_ratio": 0.0,
                "ltf_close": sub_bar_history[-1]["close"] if sub_bar_history else 0.0}

    closes = [b["close"] for b in sub_bar_history]
    volumes = [b["volume"] for b in sub_bar_history]

    # EMA
    ema = _compute_ema(closes, ema_period)
    if len(ema) >= ema_slope_bars + 1 and not math.isnan(ema[-1]) and not math.isnan(ema[-1 - ema_slope_bars]):
        ema_slope = (ema[-1] - ema[-1 - ema_slope_bars]) / max(abs(ema[-1 - ema_slope_bars]), 1e-9)
    else:
        ema_slope = 0.0

    # RSI
    rsi_arr = _compute_rsi(closes, rsi_period)
    rsi = rsi_arr[-1] if rsi_arr and not math.isnan(rsi_arr[-1]) else 50.0

    # Volume ratio
    recent_vol = volumes[-1]
    avg_vol = sum(volumes[-volume_lookback:]) / min(len(volumes), volume_lookback) if volumes else 1.0
    volume_ratio = recent_vol / avg_vol if avg_vol > 0 else 0.0

    ltf_close = closes[-1]

    # Evaluate
    if side == "buy":
        ema_aligned = ema_slope > ema_slope_min
        rsi_ok = rsi_min_long <= rsi <= rsi_max_long
        voided = rsi > rsi_void_long
    else:
        ema_aligned = ema_slope < -ema_slope_min
        rsi_ok = rsi_min_short <= rsi <= rsi_max_short
        voided = rsi < rsi_void_short

    volume_ok = volume_ratio >= volume_ratio_min

    void_reason = None
    if voided:
        void_reason = f"RSI {rsi:.1f} contradicts {side} (anti-churn)"

    confirmed = (ema_aligned and rsi_ok and volume_ok) and not voided

    return {
        "confirmed": confirmed,
        "voided": voided,
        "void_reason": void_reason,
        "rsi": round(rsi, 2),
        "ema_slope": round(ema_slope, 6),
        "volume_ratio": round(volume_ratio, 4),
        "ltf_close": round(ltf_close, 8),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Candidate Lifecycle Metrics
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CandidateLifecycleMetrics:
    """Observability metrics for the staged candidate system."""
    total_created:   int = 0
    total_confirmed: int = 0
    total_executed:  int = 0
    total_voided:    int = 0
    total_expired:   int = 0

    # Timing (in sub-bar units = 15m intervals)
    confirmation_delays: list = field(default_factory=list)   # CREATED→CONFIRMED in sub-bars
    candidate_ages:      list = field(default_factory=list)   # time in CREATED state (sub-bars)

    # Execution clustering
    executions_per_cycle: list = field(default_factory=list)   # how many exec per 15m cycle
    executions_per_symbol_per_cycle: list = field(default_factory=list)

    # Validation audit
    audit_log: list = field(default_factory=list)  # all state transitions

    @property
    def conversion_rate(self) -> float:
        """created → executed rate."""
        return self.total_executed / self.total_created if self.total_created > 0 else 0.0

    @property
    def expiry_rate(self) -> float:
        """% of candidates expiring without confirmation."""
        return self.total_expired / self.total_created if self.total_created > 0 else 0.0

    @property
    def void_rate(self) -> float:
        return self.total_voided / self.total_created if self.total_created > 0 else 0.0

    @property
    def avg_confirmation_delay(self) -> float:
        """Average delay in sub-bars (15m units)."""
        return sum(self.confirmation_delays) / len(self.confirmation_delays) if self.confirmation_delays else 0.0

    @property
    def avg_candidate_age(self) -> float:
        return sum(self.candidate_ages) / len(self.candidate_ages) if self.candidate_ages else 0.0

    def to_dict(self) -> dict:
        return {
            "total_created": self.total_created,
            "total_confirmed": self.total_confirmed,
            "total_executed": self.total_executed,
            "total_voided": self.total_voided,
            "total_expired": self.total_expired,
            "conversion_rate": round(self.conversion_rate, 4),
            "expiry_rate": round(self.expiry_rate, 4),
            "void_rate": round(self.void_rate, 4),
            "avg_confirmation_delay_15m": round(self.avg_confirmation_delay, 2),
            "avg_candidate_age_15m": round(self.avg_candidate_age, 2),
            "confirmation_delay_distribution": {
                "min": min(self.confirmation_delays) if self.confirmation_delays else 0,
                "max": max(self.confirmation_delays) if self.confirmation_delays else 0,
                "median": sorted(self.confirmation_delays)[len(self.confirmation_delays) // 2] if self.confirmation_delays else 0,
            },
            "execution_clustering": {
                "total_cycles_with_executions": sum(1 for x in self.executions_per_cycle if x > 0),
                "max_executions_per_cycle": max(self.executions_per_cycle) if self.executions_per_cycle else 0,
                "avg_executions_per_cycle": (
                    sum(self.executions_per_cycle) / len(self.executions_per_cycle)
                    if self.executions_per_cycle else 0.0
                ),
                "burst_cycles": sum(1 for x in self.executions_per_cycle if x > 1),
            },
        }


# ─────────────────────────────────────────────────────────────────────────────
# Validation Rules Checker
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ValidationResult:
    """Results of structural validation checks on the candidate lifecycle."""
    no_execution_before_confirmation: bool = True
    no_duplicate_executions: bool = True
    terminal_states_respected: bool = True
    violations: list = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return (self.no_execution_before_confirmation
                and self.no_duplicate_executions
                and self.terminal_states_respected)


def _validate_candidates(candidates: list[_SimCandidate]) -> ValidationResult:
    """Check structural invariants on all candidates."""
    result = ValidationResult()

    executed_ids = set()
    for c in candidates:
        # Rule 1: No execution before confirmation
        if c.state == _CandidateState.EXECUTED:
            if c.confirmed_at_bar < 0:
                result.no_execution_before_confirmation = False
                result.violations.append(
                    f"CID-{c.cid}: EXECUTED without CONFIRMED "
                    f"(confirmed_at_bar={c.confirmed_at_bar})"
                )
            # Must have been confirmed before or at execution bar
            if c.confirmed_at_bar > c.executed_at_bar:
                result.no_execution_before_confirmation = False
                result.violations.append(
                    f"CID-{c.cid}: confirmed_at_bar({c.confirmed_at_bar}) > "
                    f"executed_at_bar({c.executed_at_bar})"
                )

        # Rule 2: No duplicate execution
        if c.state == _CandidateState.EXECUTED:
            if c.cid in executed_ids:
                result.no_duplicate_executions = False
                result.violations.append(
                    f"CID-{c.cid}: executed more than once"
                )
            executed_ids.add(c.cid)

        # Rule 3: Terminal states respected
        if c.state == _CandidateState.VOIDED and c.executed_at_bar >= 0:
            result.terminal_states_respected = False
            result.violations.append(
                f"CID-{c.cid}: VOIDED but has executed_at_bar={c.executed_at_bar}"
            )
        if c.state == _CandidateState.EXPIRED and c.executed_at_bar >= 0:
            result.terminal_states_respected = False
            result.violations.append(
                f"CID-{c.cid}: EXPIRED but has executed_at_bar={c.executed_at_bar}"
            )

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Staged Backtester
# ─────────────────────────────────────────────────────────────────────────────

class StagedBacktester:
    """
    Dual-mode backtester that supports both baseline (immediate execution)
    and staged (HTF signal → LTF confirmation) modes.

    Parameters
    ----------
    staged : bool
        If True, run with 15m confirmation cycles.
        If False, run baseline (immediate execution, identical to EnhancedIDSSBacktester).
    expiry_bars : int
        How many 1H bars before a CREATED candidate expires (default 4 = 4 hours).
    max_ltf_history : int
        Maximum 15m sub-bars to retain for LTF indicator calculation.
    """

    def __init__(
        self,
        staged: bool = True,
        expiry_bars: int = 4,
        max_ltf_history: int = 100,
        min_confluence_score: float | None = None,
        warmup_bars: int = 100,
        seed: int = 42,
        # LTF confirmation parameters (Phase 6 tuning)
        ltf_config: dict | None = None,
    ):
        self._staged = staged
        self._expiry_bars = expiry_bars
        self._max_ltf_history = max_ltf_history
        self._seed = seed
        # LTF config: dict of kwargs passed to _evaluate_ltf_confirmation
        # Keys: ema_slope_bars, ema_slope_min, rsi_max_long, rsi_min_long,
        #        rsi_min_short, rsi_max_short, rsi_void_long, rsi_void_short,
        #        volume_ratio_min
        self._ltf_config = ltf_config or {}

        from config.settings import settings as _s
        self._threshold = min_confluence_score if min_confluence_score is not None \
                          else float(_s.get("idss.min_confluence_score", 0.45))
        self._warmup_bars = warmup_bars

        from core.regime.regime_classifier     import RegimeClassifier
        from core.signals.signal_generator     import SignalGenerator
        from core.meta_decision.confluence_scorer import ConfluenceScorer
        from core.meta_decision.position_sizer import PositionSizer
        from core.risk.risk_gate               import RiskGate

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
        df: pd.DataFrame,
        symbol: str,
        timeframe: str = "1h",
        initial_capital: float = 10_000.0,
        fee_pct: float = 0.10,
        slippage_pct: float = 0.05,
        spread_pct: float = 0.05,
        progress_cb: Optional[Callable[[str], None]] = None,
    ) -> dict:
        """
        Run bar-by-bar backtest.

        In staged mode, each 1H bar is decomposed into 4 × 15m sub-bars.
        LTF confirmation is evaluated on each sub-bar cycle.
        """
        if df is None or df.empty:
            return self._empty_result(symbol, timeframe, initial_capital)

        rng = np.random.default_rng(self._seed)

        n      = len(df)
        fee    = fee_pct / 100.0
        slip   = slippage_pct / 100.0
        spread = spread_pct / 100.0

        equity         = initial_capital
        position       = None
        equity_curve   = [initial_capital]
        chart_equity   = [initial_capital]
        eq_timestamps  = [df.index[0]]
        trades: list[dict] = []

        # Staged-mode state
        candidates: list[_SimCandidate] = []
        all_candidates: list[_SimCandidate] = []
        cid_counter = 0
        sub_bar_history: list[dict] = []  # running 15m history for LTF indicators
        lifecycle = CandidateLifecycleMetrics()
        global_sub_bar = 0  # total 15m sub-bars elapsed

        _report_every = max(1, n // 20)

        for i in range(1, n):
            row = df.iloc[i]
            ts  = df.index[i]

            if progress_cb and i % _report_every == 0:
                pct = int(i / n * 100)
                progress_cb(f"  {symbol} [{timeframe}] bar {i:,}/{n:,} ({pct}%)")

            # ── Check SL / TP ────────────────────────────────────
            if position is not None:
                direction = position["direction"]
                triggered = False
                exit_reason = "end_of_data"
                exit_px = float(row["close"])

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
                    trade_dict = self._close_position(
                        position, exit_px, exit_reason, direction,
                        slip, spread, fee, i, ts, symbol, slippage_pct, df,
                    )
                    pnl = trade_dict["pnl"]
                    equity += pnl
                    trades.append(trade_dict)
                    equity_curve.append(round(equity, 4))
                    position = None

            # ── Run IDSS pipeline (HTF signal generation) ────────
            candidate_generated = None
            if position is None and i >= self._warmup_bars:
                candidate_generated = self._run_pipeline(df.iloc[:i + 1], symbol, timeframe)

            if not self._staged:
                # ── BASELINE MODE: immediate execution ───────────
                if candidate_generated is not None and position is None:
                    drawdown_pct = max(0.0, (1.0 - equity / initial_capital) * 100.0)
                    if self._passes_risk(candidate_generated, equity, drawdown_pct):
                        position = self._open_position(
                            candidate_generated, row, i, ts, equity,
                            drawdown_pct, slip, spread, fee,
                        )
                        if position:
                            equity -= position["entry_fee"]
            else:
                # ── STAGED MODE: create candidate, defer execution ──
                # Step 1: Expire stale candidates
                for c in candidates:
                    if c.state == _CandidateState.CREATED:
                        if (i - c.created_at_bar) >= self._expiry_bars:
                            c.expire(i)
                            lifecycle.total_expired += 1
                            age = (i - c.created_at_bar) * 4  # in sub-bars
                            lifecycle.candidate_ages.append(age)
                            lifecycle.audit_log.append({
                                "cid": c.cid, "event": "EXPIRED",
                                "bar": i, "age_bars": i - c.created_at_bar,
                            })

                # Step 2: Stage new candidate if generated
                if candidate_generated is not None:
                    fp = _make_fingerprint(
                        symbol, candidate_generated.side,
                        candidate_generated.models_fired,
                        candidate_generated.regime,
                    )
                    # Dedup: refresh if same fingerprint already active
                    existing = None
                    for c in candidates:
                        if c.is_active and c.fingerprint == fp:
                            existing = c
                            break

                    if existing and existing.state == _CandidateState.CREATED:
                        # Refresh: update score and reset expiry
                        existing.score = candidate_generated.score
                        existing.entry_price = candidate_generated.entry_price
                        existing.stop_loss = candidate_generated.stop_loss_price
                        existing.take_profit = candidate_generated.take_profit_price
                        existing.created_at_bar = i
                        lifecycle.audit_log.append({
                            "cid": existing.cid, "event": "REFRESHED",
                            "bar": i, "score": existing.score,
                        })
                    elif existing is None:
                        # Create new
                        sc = _SimCandidate(
                            cid=cid_counter,
                            symbol=symbol,
                            side=candidate_generated.side,
                            entry_price=candidate_generated.entry_price,
                            stop_loss=candidate_generated.stop_loss_price,
                            take_profit=candidate_generated.take_profit_price,
                            score=candidate_generated.score,
                            models_fired=list(candidate_generated.models_fired),
                            regime=candidate_generated.regime or "uncertain",
                            atr_value=candidate_generated.atr_value or 0.0,
                            fingerprint=fp,
                            created_at_bar=i,
                        )
                        candidates.append(sc)
                        all_candidates.append(sc)
                        cid_counter += 1
                        lifecycle.total_created += 1
                        lifecycle.audit_log.append({
                            "cid": sc.cid, "event": "CREATED",
                            "bar": i, "side": sc.side, "score": sc.score,
                        })

                # Step 3: Generate 15m sub-bars and run LTF confirmation cycles
                sub_bars = interpolate_15m_bars(row, rng)
                cycle_executions = 0

                for sub_idx, sub_bar in enumerate(sub_bars):
                    global_sub_bar += 1
                    sub_bar_history.append(sub_bar)
                    if len(sub_bar_history) > self._max_ltf_history:
                        sub_bar_history = sub_bar_history[-self._max_ltf_history:]

                    # Evaluate CREATED candidates
                    for c in candidates:
                        if c.state != _CandidateState.CREATED:
                            continue

                        result = _evaluate_ltf_confirmation(
                            sub_bar_history, c.side,
                            **self._ltf_config,
                        )

                        if result["voided"]:
                            c.void(i, result["void_reason"] or "LTF void")
                            lifecycle.total_voided += 1
                            age = (i - c.created_at_bar) * 4 + sub_idx
                            lifecycle.candidate_ages.append(age)
                            lifecycle.audit_log.append({
                                "cid": c.cid, "event": "VOIDED",
                                "bar": i, "sub": sub_idx,
                                "reason": result["void_reason"],
                            })
                        elif result["confirmed"]:
                            delay = (i - c.created_at_bar) * 4 + sub_idx - c.created_at_sub
                            c.confirm(i, sub_idx,
                                      ltf_rsi=result["rsi"],
                                      ltf_ema_slope=result["ema_slope"],
                                      ltf_volume_ratio=result["volume_ratio"],
                                      ltf_close=result["ltf_close"])
                            lifecycle.total_confirmed += 1
                            lifecycle.confirmation_delays.append(delay)
                            lifecycle.audit_log.append({
                                "cid": c.cid, "event": "CONFIRMED",
                                "bar": i, "sub": sub_idx, "delay": delay,
                            })

                    # Execute CONFIRMED candidates (one per cycle max)
                    if position is None:
                        for c in candidates:
                            if c.state != _CandidateState.CONFIRMED:
                                continue
                            drawdown_pct = max(0.0, (1.0 - equity / initial_capital) * 100.0)
                            # Build a mock OrderCandidate-like for risk gate
                            if not self._passes_risk_from_sim(c, equity, drawdown_pct):
                                continue

                            # Use LTF close as execution price (more realistic)
                            exec_price = c.ltf_close if c.ltf_close > 0 else float(row["close"])
                            position = self._open_position_from_sim(
                                c, exec_price, i, ts, equity,
                                drawdown_pct, slip, spread, fee,
                            )
                            if position:
                                equity -= position["entry_fee"]
                                c.execute(i)
                                lifecycle.total_executed += 1
                                age = (i - c.created_at_bar) * 4 + sub_idx
                                lifecycle.candidate_ages.append(age)
                                lifecycle.audit_log.append({
                                    "cid": c.cid, "event": "EXECUTED",
                                    "bar": i, "sub": sub_idx,
                                })
                                cycle_executions += 1
                                break  # one execution per sub-bar cycle

                    lifecycle.executions_per_cycle.append(cycle_executions if sub_idx == 3 else 0)

                # Clean up terminal candidates older than 10 bars
                candidates = [c for c in candidates if c.is_active or (i - c.created_at_bar) < 10]

            # ── Mark-to-market for equity chart ───────────────────
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

        # ── Force-close any open position at end ──────────────────
        if position is not None:
            last = df.iloc[-1]
            close_px = float(last["close"])
            direction = position["direction"]
            trade_dict = self._close_position(
                position, close_px, "end_of_data", direction,
                slip, spread, fee, n - 1, df.index[-1], symbol, slippage_pct, df,
            )
            pnl = trade_dict["pnl"]
            equity += pnl
            trades.append(trade_dict)
            equity_curve.append(round(equity, 4))

        # ── Compute metrics ───────────────────────────────────────
        from core.backtesting.idss_backtester import _calc_metrics
        metrics = _calc_metrics(trades, equity_curve, initial_capital)

        # Add expectancy
        if trades:
            r_values = [t.get("realized_r_multiple", 0.0) for t in trades]
            metrics["expectancy_r"] = round(sum(r_values) / len(r_values), 4)
        else:
            metrics["expectancy_r"] = 0.0

        # Validate lifecycle (staged mode only)
        validation = None
        if self._staged:
            validation = _validate_candidates(all_candidates)
            # Record execution clustering per 1H cycle
            lifecycle.executions_per_cycle = []
            for bar_i in range(1, n):
                execs_this_bar = sum(
                    1 for c in all_candidates
                    if c.state == _CandidateState.EXECUTED and c.executed_at_bar == bar_i
                )
                lifecycle.executions_per_cycle.append(execs_this_bar)

        return {
            "trades":              trades,
            "equity_curve":        equity_curve,
            "chart_equity":        chart_equity,
            "equity_timestamps":   [str(t) for t in eq_timestamps],
            "metrics":             metrics,
            "symbol":              symbol,
            "timeframe":           timeframe,
            "initial_capital":     initial_capital,
            "mode":                "staged" if self._staged else "baseline",
            # Staged-mode observability
            "candidate_lifecycle": lifecycle.to_dict() if self._staged else None,
            "validation":          {
                "all_passed":                       validation.all_passed,
                "no_execution_before_confirmation":  validation.no_execution_before_confirmation,
                "no_duplicate_executions":           validation.no_duplicate_executions,
                "terminal_states_respected":         validation.terminal_states_respected,
                "violations":                        validation.violations,
            } if validation else None,
            "all_candidates":      [
                {
                    "cid": c.cid, "symbol": c.symbol, "side": c.side,
                    "state": c.state, "score": c.score,
                    "created_at_bar": c.created_at_bar,
                    "confirmed_at_bar": c.confirmed_at_bar,
                    "executed_at_bar": c.executed_at_bar,
                    "voided_at_bar": c.voided_at_bar,
                    "expired_at_bar": c.expired_at_bar,
                    "void_reason": c.void_reason,
                    "ltf_rsi": c.ltf_rsi,
                    "ltf_ema_slope": c.ltf_ema_slope,
                    "ltf_volume_ratio": c.ltf_volume_ratio,
                }
                for c in all_candidates
            ] if self._staged else None,
        }

    # ─── Pipeline helpers ─────────────────────────────────────────

    def _run_pipeline(self, df_window, symbol, timeframe):
        try:
            regime, _conf, _meta = self._regime_clf.classify(df_window)
            signals = self._sig_gen.generate(symbol, df_window, regime, timeframe)
            if not signals:
                return None
            return self._scorer.score(signals, symbol)
        except Exception as exc:
            logger.debug("StagedBacktester pipeline error: %s", exc)
            return None

    def _passes_risk(self, candidate, equity, drawdown_pct=0.0):
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

    def _passes_risk_from_sim(self, sim_cand: _SimCandidate, equity, drawdown_pct=0.0):
        """Risk-check a sim candidate by building a lightweight proxy."""
        if equity <= 0:
            return False
        ep = sim_cand.entry_price
        sl = sim_cand.stop_loss
        tp = sim_cand.take_profit
        if ep <= 0 or sl <= 0 or tp <= 0:
            return False
        if sim_cand.side == "buy"  and not (sl < ep < tp): return False
        if sim_cand.side == "sell" and not (tp < ep < sl): return False

        # Build proxy OrderCandidate
        try:
            from core.meta_decision.order_candidate import OrderCandidate
            proxy = OrderCandidate(
                symbol=sim_cand.symbol,
                side=sim_cand.side,
                entry_type="market",
                entry_price=ep,
                stop_loss_price=sl,
                take_profit_price=tp,
                position_size_usdt=0.0,
                score=sim_cand.score,
                models_fired=sim_cand.models_fired,
                regime=sim_cand.regime,
                rationale="staged_backtest",
                timeframe="1h",
                atr_value=sim_cand.atr_value,
            )
            approved, _ = self._risk_gate.validate_batch(
                [proxy], open_positions=[], available_capital=equity,
                drawdown_pct=drawdown_pct, spread_map={},
            )
            return bool(approved)
        except Exception:
            return True

    def _open_position(self, candidate, row, i, ts, equity, drawdown_pct, slip, spread, fee):
        direction = "long" if candidate.side == "buy" else "short"
        entry_fill = (
            float(row["close"]) * (1.0 + slip + spread / 2)
            if direction == "long"
            else float(row["close"]) * (1.0 - slip - spread / 2)
        )
        atr_val = candidate.atr_value or (entry_fill * 0.008)
        trade_value = self._sizer.calculate(
            available_capital_usdt=equity,
            atr_value=atr_val,
            entry_price=entry_fill,
            score=candidate.score,
            regime=candidate.regime or "uncertain",
            drawdown_pct=drawdown_pct,
        )
        if trade_value <= 0:
            return None
        qty = trade_value / entry_fill
        entry_fee = trade_value * fee
        return {
            "entry_i": i, "entry_time": ts, "entry_price": entry_fill,
            "quantity": qty, "entry_fee": entry_fee, "direction": direction,
            "sl": candidate.stop_loss_price, "tp": candidate.take_profit_price,
            "size_usdt": trade_value,
            "regime": candidate.regime, "models_fired": list(candidate.models_fired),
            "score": candidate.score, "wf_window": -1,
        }

    def _open_position_from_sim(self, sim_cand, exec_price, i, ts, equity,
                                 drawdown_pct, slip, spread, fee):
        direction = "long" if sim_cand.side == "buy" else "short"
        entry_fill = (
            exec_price * (1.0 + slip + spread / 2)
            if direction == "long"
            else exec_price * (1.0 - slip - spread / 2)
        )
        atr_val = sim_cand.atr_value or (entry_fill * 0.008)
        trade_value = self._sizer.calculate(
            available_capital_usdt=equity,
            atr_value=atr_val,
            entry_price=entry_fill,
            score=sim_cand.score,
            regime=sim_cand.regime or "uncertain",
            drawdown_pct=drawdown_pct,
        )
        if trade_value <= 0:
            return None
        qty = trade_value / entry_fill
        entry_fee = trade_value * fee
        return {
            "entry_i": i, "entry_time": ts, "entry_price": entry_fill,
            "quantity": qty, "entry_fee": entry_fee, "direction": direction,
            "sl": sim_cand.stop_loss, "tp": sim_cand.take_profit,
            "size_usdt": trade_value,
            "regime": sim_cand.regime, "models_fired": list(sim_cand.models_fired),
            "score": sim_cand.score, "wf_window": -1,
            "ltf_confirmed": True,
            "ltf_close": sim_cand.ltf_close,
        }

    def _close_position(self, position, exit_px, exit_reason, direction,
                         slip, spread, fee, i, ts, symbol, slippage_pct, df):
        exit_fill = (
            exit_px * (1.0 - slip - spread / 2) if direction == "long"
            else exit_px * (1.0 + slip + spread / 2)
        )
        qty = position["quantity"]
        exit_fee = exit_fill * qty * fee
        if direction == "long":
            pnl = (exit_fill - position["entry_price"]) * qty - exit_fee - position["entry_fee"]
        else:
            pnl = (position["entry_price"] - exit_fill) * qty - exit_fee - position["entry_fee"]

        pnl_pct = pnl / (position["entry_price"] * qty) * 100.0

        entry_px  = position["entry_price"]
        sl_px     = position["sl"]
        tp_px     = position["tp"]
        size_usdt = position["size_usdt"]
        risk_usdt = abs(entry_px - sl_px) / entry_px * size_usdt
        real_r    = round(pnl / risk_usdt, 4) if risk_usdt > 0 else 0.0
        sl_dist   = abs(entry_px - sl_px)
        exp_rr    = round(abs(tp_px - entry_px) / sl_dist, 2) if sl_dist > 0 else 0.0

        try:
            entry_dt = pd.Timestamp(position["entry_time"])
            exit_dt  = pd.Timestamp(ts)
            dur_h = (exit_dt - entry_dt).total_seconds() / 3600.0
        except Exception:
            dur_h = 0.0

        true_regime = ""
        if "true_regime" in df.columns:
            true_regime = str(df.iloc[i].get("true_regime", ""))

        return {
            "symbol": symbol,
            "entry_time": str(position["entry_time"]),
            "exit_time": str(ts),
            "entry_price": round(entry_px, 8),
            "exit_price": round(exit_fill, 8),
            "stop_price": round(sl_px, 8),
            "tp_price": round(tp_px, 8),
            "quantity": round(qty, 8),
            "size_usdt": round(size_usdt, 4),
            "direction": direction,
            "pnl": round(pnl, 4),
            "pnl_usdt": round(pnl, 4),
            "pnl_pct": round(pnl_pct, 2),
            "exit_reason": exit_reason,
            "duration_bars": i - position["entry_i"],
            "duration_hours": round(dur_h, 2),
            "regime": position.get("regime", "") or true_regime,
            "regime_at_entry": position.get("regime", "") or true_regime,
            "models_fired": position.get("models_fired", []),
            "confluence_score": position.get("score", 0.0),
            "score": position.get("score", 0.0),
            "realized_r_multiple": real_r,
            "expected_rr": exp_rr,
            "slippage_estimate": round(slippage_pct, 4),
            "slippage_pct": round(slippage_pct, 4),
            "wf_window": position.get("wf_window", -1),
            "ltf_confirmed": position.get("ltf_confirmed", not self._staged),
        }

    @staticmethod
    def _empty_result(symbol, timeframe, initial_capital):
        from core.backtesting.idss_backtester import _calc_metrics
        return {
            "trades": [], "equity_curve": [], "chart_equity": [],
            "equity_timestamps": [], "metrics": _calc_metrics([], [], initial_capital),
            "symbol": symbol, "timeframe": timeframe, "initial_capital": initial_capital,
            "mode": "unknown", "candidate_lifecycle": None,
            "validation": None, "all_candidates": None,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Comparison Runner
# ─────────────────────────────────────────────────────────────────────────────

def run_ab_comparison(
    symbols: list[str] | None = None,
    timeframe: str = "1h",
    initial_capital: float = 10_000.0,
    seed: int = 42,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> dict:
    """
    Run baseline vs staged backtest on same synthetic data.

    Returns dict with:
      baseline: {per_symbol: {}, aggregate_metrics: {}}
      staged:   {per_symbol: {}, aggregate_metrics: {}, candidate_lifecycle: {}, validation: {}}
      comparison: {metric_deltas}
    """
    from core.features.indicator_library import calculate_all

    symbols = symbols or [
        "BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT", "SOL/USDT",
        "TRX/USDT", "DOGE/USDT", "ADA/USDT", "BCH/USDT", "HYPE/USDT",
        "LINK/USDT", "XLM/USDT", "AVAX/USDT", "HBAR/USDT", "SUI/USDT",
        "NEAR/USDT", "ICP/USDT", "ONDO/USDT", "ALGO/USDT", "RENDER/USDT",
    ]
    gen = SyntheticRegimeDataGenerator(seed=seed)

    # Generate 1H data for all symbols
    ohlcv_map = {}
    for sym in symbols:
        df_raw, _ = gen.generate(sym, timeframe=timeframe, start_date="2024-01-01")
        try:
            df_ind = calculate_all(df_raw)
        except Exception:
            df_ind = df_raw
        ohlcv_map[sym] = df_ind

    def _log(msg):
        logger.info(msg)
        if progress_cb:
            progress_cb(msg)

    # ── Run BASELINE ──────────────────────────────────────────────
    _log("=" * 60)
    _log("BASELINE RUN (immediate execution)")
    _log("=" * 60)

    baseline_bt = StagedBacktester(staged=False, seed=seed)
    baseline_results = {}
    baseline_all_trades = []

    for sym in symbols:
        _log(f"  Baseline: {sym}")
        result = baseline_bt.run(
            ohlcv_map[sym], sym, timeframe, initial_capital,
            progress_cb=lambda m: _log(f"    {m}"),
        )
        baseline_results[sym] = result
        baseline_all_trades.extend(result["trades"])

    # ── Run STAGED ────────────────────────────────────────────────
    _log("=" * 60)
    _log("STAGED RUN (15m confirmation)")
    _log("=" * 60)

    staged_bt = StagedBacktester(staged=True, seed=seed)
    staged_results = {}
    staged_all_trades = []
    staged_all_lifecycle = CandidateLifecycleMetrics()
    staged_all_candidates_list = []
    staged_validations = []

    for sym in symbols:
        _log(f"  Staged: {sym}")
        result = staged_bt.run(
            ohlcv_map[sym], sym, timeframe, initial_capital,
            progress_cb=lambda m: _log(f"    {m}"),
        )
        staged_results[sym] = result
        staged_all_trades.extend(result["trades"])

        # Aggregate lifecycle
        if result["candidate_lifecycle"]:
            lc = result["candidate_lifecycle"]
            staged_all_lifecycle.total_created += lc["total_created"]
            staged_all_lifecycle.total_confirmed += lc["total_confirmed"]
            staged_all_lifecycle.total_executed += lc["total_executed"]
            staged_all_lifecycle.total_voided += lc["total_voided"]
            staged_all_lifecycle.total_expired += lc["total_expired"]

        if result["all_candidates"]:
            staged_all_candidates_list.extend(result["all_candidates"])

        if result["validation"]:
            staged_validations.append(result["validation"])

    # Aggregate confirmation delays across symbols
    for sym in symbols:
        r = staged_results[sym]
        if r.get("candidate_lifecycle"):
            # Re-read per-symbol lifecycle (it's a dict, not the dataclass)
            pass  # Delays already in per-symbol results

    # ── Compute aggregate metrics ─────────────────────────────────
    from core.backtesting.idss_backtester import _calc_metrics

    b_eq = [initial_capital]
    for t in sorted(baseline_all_trades, key=lambda x: x.get("exit_time", "")):
        b_eq.append(b_eq[-1] + t["pnl"])
    baseline_metrics = _calc_metrics(baseline_all_trades, b_eq, initial_capital)
    if baseline_all_trades:
        baseline_metrics["expectancy_r"] = round(
            sum(t.get("realized_r_multiple", 0) for t in baseline_all_trades)
            / len(baseline_all_trades), 4
        )
    else:
        baseline_metrics["expectancy_r"] = 0.0

    s_eq = [initial_capital]
    for t in sorted(staged_all_trades, key=lambda x: x.get("exit_time", "")):
        s_eq.append(s_eq[-1] + t["pnl"])
    staged_metrics = _calc_metrics(staged_all_trades, s_eq, initial_capital)
    if staged_all_trades:
        staged_metrics["expectancy_r"] = round(
            sum(t.get("realized_r_multiple", 0) for t in staged_all_trades)
            / len(staged_all_trades), 4
        )
    else:
        staged_metrics["expectancy_r"] = 0.0

    # ── Compute deltas ────────────────────────────────────────────
    def _pct_change(new, old):
        if old == 0:
            return float("inf") if new != 0 else 0.0
        return round((new - old) / abs(old) * 100, 2)

    comparison = {
        "profit_factor_baseline": baseline_metrics.get("profit_factor", 0),
        "profit_factor_staged": staged_metrics.get("profit_factor", 0),
        "profit_factor_change_pct": _pct_change(
            staged_metrics.get("profit_factor", 0),
            baseline_metrics.get("profit_factor", 0),
        ),
        "max_drawdown_baseline": baseline_metrics.get("max_drawdown_pct", 0),
        "max_drawdown_staged": staged_metrics.get("max_drawdown_pct", 0),
        "max_drawdown_change_pct": _pct_change(
            staged_metrics.get("max_drawdown_pct", 0),
            baseline_metrics.get("max_drawdown_pct", 0),
        ),
        "expectancy_baseline": baseline_metrics.get("expectancy_r", 0),
        "expectancy_staged": staged_metrics.get("expectancy_r", 0),
        "expectancy_change_pct": _pct_change(
            staged_metrics.get("expectancy_r", 0),
            baseline_metrics.get("expectancy_r", 0),
        ),
        "win_rate_baseline": baseline_metrics.get("win_rate", 0),
        "win_rate_staged": staged_metrics.get("win_rate", 0),
        "trade_count_baseline": baseline_metrics.get("total_trades", 0),
        "trade_count_staged": staged_metrics.get("total_trades", 0),
        "total_return_baseline": baseline_metrics.get("total_return_pct", 0),
        "total_return_staged": staged_metrics.get("total_return_pct", 0),
    }

    # ── Aggregate validation ──────────────────────────────────────
    all_validation_passed = all(v["all_passed"] for v in staged_validations) if staged_validations else True
    all_violations = []
    for v in staged_validations:
        all_violations.extend(v.get("violations", []))

    return {
        "baseline": {
            "per_symbol": {s: baseline_results[s]["metrics"] for s in symbols},
            "aggregate_metrics": baseline_metrics,
            "all_trades": baseline_all_trades,
        },
        "staged": {
            "per_symbol": {s: staged_results[s]["metrics"] for s in symbols},
            "aggregate_metrics": staged_metrics,
            "all_trades": staged_all_trades,
            "candidate_lifecycle": staged_all_lifecycle.to_dict(),
            "per_symbol_lifecycle": {
                s: staged_results[s].get("candidate_lifecycle")
                for s in symbols
            },
            "per_symbol_candidates": {
                s: staged_results[s].get("all_candidates")
                for s in symbols
            },
        },
        "comparison": comparison,
        "validation": {
            "all_passed": all_validation_passed,
            "violations": all_violations,
        },
        "symbols": symbols,
        "timeframe": timeframe,
        "initial_capital": initial_capital,
    }


# Re-use the existing SyntheticRegimeDataGenerator
from core.validation.walk_forward_regime_validator import SyntheticRegimeDataGenerator
