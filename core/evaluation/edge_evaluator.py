# ============================================================
# NEXUS TRADER — Edge Evaluator
#
# Measures whether NexusTrader has a genuine, statistically
# credible trading edge — independently of the existing
# DemoPerformanceEvaluator readiness checklist.
#
# The two evaluators are complementary:
#   DemoPerformanceEvaluator — "is the system stable enough?"
#   EdgeEvaluator            — "does it actually have edge?"
#
# Core metrics
# ─────────────────────────────────────────────────────────
#   Expectancy (in R units)
#       E[R] = WinRate × AvgWinR − LossRate × AvgLossR
#
#   Profit Factor
#       PF = ΣWins_R / Σ|Losses_R|
#
#   Profit Factor Stability (PFS)
#       Built from the rolling-20 PF series.  We capture one
#       PF snapshot per trade (after trade #20), take the last
#       N_SNAP=10 snapshots, and compute their Coefficient of
#       Variation (CV = σ/μ).  A low CV means PF is stable.
#
#       PFS score = max(0, min(100, round(100 × (1 − CV))))
#         • ≥ 85 → "Stable"
#         • ≥ 60 → "Moderate"
#         • <  60 → "Unstable"
#
# SAFETY CONTRACT (unchanged from DemoPerformanceEvaluator)
# ─────────────────────────────────────────────────────────
#   This module ONLY evaluates.  It NEVER modifies the
#   OrderRouter mode, triggers live execution, or changes
#   any system setting automatically.
# ============================================================
from __future__ import annotations

import math
import logging
import statistics
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────────────

@dataclass
class EdgeThresholds:
    # ── Early viability zone (~40 trades) ──────────────────
    min_trades_early:       int   = 40
    min_pf_early:           float = 1.35
    min_expectancy_early:   float = 0.20   # R units

    # ── Stronger readiness zone (~75+ trades) ──────────────
    min_trades_full:        int   = 75
    min_pf_full:            float = 1.40
    min_expectancy_full:    float = 0.25   # R units
    min_pfs_score:          float = 60.0  # 0–100 stability score

    # ── Risk / drawdown ────────────────────────────────────
    max_drawdown_r:         float = 10.0  # max peak-to-trough in R units

    # ── Score calibration bins (diagnostic only) ───────────
    # 0.60–0.70, 0.70–0.80, 0.80–0.90, 0.90–1.00
    score_bins: list = field(default_factory=lambda: [
        (0.60, 0.70), (0.70, 0.80), (0.80, 0.90), (0.90, 1.01),
    ])

# Number of rolling-20-PF snapshots used for PFS stability calculation
_PFS_SNAP_WINDOW: int = 10


# ── Per-dimension expectancy metrics ─────────────────────────────────────────

@dataclass
class ExpectancyMetrics:
    """
    Expectancy metrics for a slice of trades (overall, rolling window,
    or a specific dimension: regime / model / asset / score bucket).

    All R values use the realized_r_multiple field (P&L / initial risk).
    """
    win_rate:       float           # fraction [0, 1]
    loss_rate:      float           # 1 − win_rate
    avg_win_r:      float           # avg realized R on winning trades
    avg_loss_r:     float           # avg abs realized R on losing trades
    expectancy_r:   float           # E[R] = WR × AvgWinR − LR × AvgLossR
    trade_count:    int
    gross_win_r:    float = 0.0     # sum of positive R values
    gross_loss_r:   float = 0.0     # sum of absolute negative R values
    profit_factor:  float = 0.0     # gross_win_r / gross_loss_r

    @property
    def edge_label(self) -> str:
        e = self.expectancy_r
        if e < 0:
            return "Losing"
        if e < 0.10:
            return "Marginal"
        if e < 0.20:
            return "Weak"
        if e < 0.30:
            return "Meaningful"
        return "Strong"


@dataclass
class ScoreBucketMetrics:
    """Score-calibration diagnostic for one confluence-score bucket."""
    bucket:       str     # e.g. "0.70-0.80"
    trade_count:  int
    win_rate:     float   # fraction
    avg_r:        float   # avg realized R across all trades in bucket
    expectancy_r: float   # E[R] for this bucket


@dataclass
class ProfitFactorMetrics:
    """Profit factor and stability across rolling windows."""
    overall:        float           # PF over all trades
    rolling_20:     Optional[float] # PF over last 20 trades
    rolling_40:     Optional[float] # PF over last 40 trades
    pfs_score:      float           # 0–100 stability score
    pfs_cv:         float           # CV of rolling-20 PF snapshots
    pfs_label:      str             # "Stable" / "Moderate" / "Unstable" / "Insufficient"
    rolling_20_history: list[float] = field(default_factory=list)  # for charting
    rolling_40_history: list[float] = field(default_factory=list)  # for charting


# ── Verdict constants ─────────────────────────────────────────────────────────

class EdgeVerdict:
    NOT_READY          = "NOT_READY"
    NEEDS_IMPROVEMENT  = "NEEDS_IMPROVEMENT"
    READY_FOR_LIVE     = "READY_FOR_LIVE"


# ── Full edge assessment ──────────────────────────────────────────────────────

@dataclass
class EdgeAssessment:
    """
    Complete edge evaluation result.

    Separate from ReadinessAssessment — this answers "does the system
    have edge?" while DemoPerformanceEvaluator answers "is it stable?".
    Both must be positive before considering live trading.
    """
    verdict:                str
    score:                  int                       # 0–100 weighted
    trade_count:            int
    overall_expectancy:     Optional[ExpectancyMetrics]
    rolling_20_expectancy:  Optional[ExpectancyMetrics]
    rolling_40_expectancy:  Optional[ExpectancyMetrics]
    profit_factor_metrics:  ProfitFactorMetrics
    drawdown_r:             float                     # peak-to-trough in R
    expectancy_by_regime:   dict[str, ExpectancyMetrics] = field(default_factory=dict)
    expectancy_by_model:    dict[str, ExpectancyMetrics] = field(default_factory=dict)
    expectancy_by_asset:    dict[str, ExpectancyMetrics] = field(default_factory=dict)
    score_calibration:      dict[str, ScoreBucketMetrics] = field(default_factory=dict)
    cumulative_r_history:   list[float] = field(default_factory=list)  # for R chart
    rolling_20_exp_history: list[float] = field(default_factory=list)  # for exp chart
    checks_passed:          list[str]   = field(default_factory=list)
    checks_failed:          list[str]   = field(default_factory=list)
    explanation:            str = ""
    generated_at:           str = ""


# ── Core computation helpers ──────────────────────────────────────────────────

def _r_from_dict(trade: dict) -> Optional[float]:
    """
    Extract or compute realized R-multiple from a trade dict.

    Priority:
      1. realized_r_multiple field (pre-computed by PaperExecutor)
      2. Computed from entry_price, stop_loss, size_usdt, pnl_usdt
    """
    if trade.get("realized_r_multiple") is not None:
        try:
            return float(trade["realized_r_multiple"])
        except (TypeError, ValueError):
            pass
    # Fall back to on-the-fly computation
    entry = float(trade.get("entry_price", 0) or 0)
    sl    = float(trade.get("stop_loss",   0) or 0)
    size  = float(trade.get("size_usdt",   0) or 0)
    pnl   = float(trade.get("pnl_usdt",    0) or 0)
    if entry > 0 and sl > 0 and size > 0:
        risk_usdt = abs(entry - sl) / entry * size
        if risk_usdt > 0:
            return round(pnl / risk_usdt, 4)
    return None


def _compute_expectancy(r_values: list[float]) -> Optional[ExpectancyMetrics]:
    """
    Compute ExpectancyMetrics from a list of realized R values.
    Returns None if there are no trades with computable R.
    """
    if not r_values:
        return None

    wins   = [r for r in r_values if r > 0]
    losses = [r for r in r_values if r <= 0]
    n      = len(r_values)

    win_rate  = len(wins)  / n
    loss_rate = len(losses) / n

    avg_win_r  = sum(wins)        / len(wins)   if wins   else 0.0
    avg_loss_r = abs(sum(losses)) / len(losses) if losses else 0.0

    expectancy = win_rate * avg_win_r - loss_rate * avg_loss_r

    gross_win_r  = sum(wins)
    gross_loss_r = abs(sum(losses))
    pf = (gross_win_r / gross_loss_r) if gross_loss_r > 0 else (
        float("inf") if gross_win_r > 0 else 0.0
    )
    pf = min(pf, 999.0)  # cap at 999 to avoid inf serialisation issues

    return ExpectancyMetrics(
        win_rate      = round(win_rate,   4),
        loss_rate     = round(loss_rate,  4),
        avg_win_r     = round(avg_win_r,  4),
        avg_loss_r    = round(avg_loss_r, 4),
        expectancy_r  = round(expectancy, 4),
        trade_count   = n,
        gross_win_r   = round(gross_win_r,  4),
        gross_loss_r  = round(gross_loss_r, 4),
        profit_factor = round(pf, 4),
    )


def _compute_pf_for_r_list(r_values: list[float]) -> float:
    """Profit factor from a list of R values."""
    gw = sum(r for r in r_values if r > 0)
    gl = abs(sum(r for r in r_values if r <= 0))
    if gl == 0:
        return 999.0 if gw > 0 else 0.0
    return round(gw / gl, 4)


def _compute_drawdown_r(r_values: list[float]) -> float:
    """
    Peak-to-trough drawdown in R units from a sequence of realized R values.
    """
    if not r_values:
        return 0.0
    cumulative = []
    total = 0.0
    for r in r_values:
        total += r
        cumulative.append(total)
    peak   = cumulative[0]
    max_dd = 0.0
    for v in cumulative:
        if v > peak:
            peak = v
        dd = peak - v
        if dd > max_dd:
            max_dd = dd
    return round(max_dd, 4)


def _compute_pf_series(r_values: list[float], window: int) -> list[float]:
    """
    Rolling PF series: one value per trade, starting at index `window-1`.
    Returns a list of length max(0, len(r_values) - window + 1).
    """
    result = []
    for i in range(window - 1, len(r_values)):
        chunk = r_values[i - window + 1 : i + 1]
        result.append(_compute_pf_for_r_list(chunk))
    return result


def _compute_rolling_exp_series(r_values: list[float], window: int) -> list[float]:
    """
    Rolling expectancy series: one E[R] value per trade, starting at index `window-1`.
    """
    result = []
    for i in range(window - 1, len(r_values)):
        chunk = r_values[i - window + 1 : i + 1]
        em = _compute_expectancy(chunk)
        result.append(em.expectancy_r if em else 0.0)
    return result


def _pfs_from_pf_series(pf_series: list[float]) -> tuple[float, float, str]:
    """
    Compute PFS score from a series of rolling-20 PF snapshots.

    Takes the last _PFS_SNAP_WINDOW values from pf_series.
    Returns (pfs_score 0–100, cv, label).
    """
    snap = pf_series[-_PFS_SNAP_WINDOW:] if len(pf_series) >= 5 else pf_series
    if len(snap) < 5:
        return 0.0, 0.0, "Insufficient data"

    # Filter out inf/999 extremes for cleaner stats
    finite_snap = [min(v, 5.0) for v in snap]
    mean_pf = statistics.mean(finite_snap)
    if mean_pf <= 0:
        return 0.0, 0.0, "Insufficient data"

    std_pf = statistics.stdev(finite_snap) if len(finite_snap) >= 2 else 0.0
    cv     = std_pf / mean_pf

    score  = max(0.0, min(100.0, round(100.0 * (1.0 - cv), 1)))

    if score >= 85:
        label = "Stable"
    elif score >= 60:
        label = "Moderate"
    else:
        label = "Unstable"

    return score, round(cv, 4), label


# ── Main evaluator ────────────────────────────────────────────────────────────

class EdgeEvaluator:
    """
    Evaluates trading edge quality from completed trade history.

    Accepts the same trade-dict format as DemoPerformanceEvaluator
    (produced by PaperExecutor.get_closed_trades()).

    Usage
    ─────
    evaluator = EdgeEvaluator()
    assessment = evaluator.evaluate(closed_trades)
    print(assessment.verdict)
    print(assessment.overall_expectancy.expectancy_r)

    SAFETY: This class NEVER switches trading modes automatically.
    """

    def __init__(self, thresholds: Optional[EdgeThresholds] = None):
        self._thresholds = thresholds or EdgeThresholds()
        self._lock       = threading.Lock()
        self._last:      Optional[EdgeAssessment] = None

    @property
    def thresholds(self) -> EdgeThresholds:
        return self._thresholds

    # ── Public API ────────────────────────────────────────────────────────────

    def evaluate(
        self, closed_trades: Optional[list[dict]] = None
    ) -> EdgeAssessment:
        """
        Run all edge checks and return an EdgeAssessment.

        If closed_trades is None, fetches from the paper_executor singleton.
        """
        with self._lock:
            if closed_trades is None:
                closed_trades = self._fetch_trades()
            assessment = self._run_evaluation(closed_trades)
            self._last = assessment
            return assessment

    def last_assessment(self) -> Optional[EdgeAssessment]:
        """Return the most recent assessment without re-evaluating."""
        with self._lock:
            return self._last

    # ── Internal ──────────────────────────────────────────────────────────────

    @staticmethod
    def _fetch_trades() -> list[dict]:
        try:
            from core.execution.paper_executor import paper_executor
            return paper_executor.get_closed_trades()
        except Exception as exc:
            logger.warning("EdgeEvaluator: could not fetch trades — %s", exc)
            return []

    def _run_evaluation(self, trades: list[dict]) -> EdgeAssessment:
        t   = self._thresholds
        n   = len(trades)

        if n == 0:
            return EdgeAssessment(
                verdict               = EdgeVerdict.NOT_READY,
                score                 = 0,
                trade_count           = 0,
                overall_expectancy    = None,
                rolling_20_expectancy = None,
                rolling_40_expectancy = None,
                profit_factor_metrics = ProfitFactorMetrics(
                    overall=0.0, rolling_20=None, rolling_40=None,
                    pfs_score=0.0, pfs_cv=0.0, pfs_label="Insufficient data",
                ),
                drawdown_r            = 0.0,
                checks_failed         = ["No completed trades"],
                explanation           = self._explain_not_ready_empty(),
                generated_at          = _now_utc(),
            )

        # ── Sort chronologically ─────────────────────────────────────────
        sorted_trades = sorted(trades, key=lambda x: x.get("closed_at", ""))

        # ── Build R value sequence ───────────────────────────────────────
        r_seq: list[float] = []
        for tr in sorted_trades:
            r = _r_from_dict(tr)
            if r is not None:
                r_seq.append(r)

        n_r = len(r_seq)   # may differ from n if some trades lack R data

        # ── Overall metrics ──────────────────────────────────────────────
        overall_exp  = _compute_expectancy(r_seq)
        rolling_20   = _compute_expectancy(r_seq[-20:]) if n_r >= 20 else None
        rolling_40   = _compute_expectancy(r_seq[-40:]) if n_r >= 40 else None

        # ── PF metrics ───────────────────────────────────────────────────
        overall_pf  = _compute_pf_for_r_list(r_seq)
        pf_20       = _compute_pf_for_r_list(r_seq[-20:]) if n_r >= 20 else None
        pf_40       = _compute_pf_for_r_list(r_seq[-40:]) if n_r >= 40 else None

        # Rolling PF history for charting (one value per trade after first 20)
        pf_20_history = _compute_pf_series(r_seq, 20)
        pf_40_history = _compute_pf_series(r_seq, 40)
        pfs_score, pfs_cv, pfs_label = _pfs_from_pf_series(pf_20_history)

        pf_metrics = ProfitFactorMetrics(
            overall             = round(min(overall_pf, 999.0), 4),
            rolling_20          = round(pf_20,  4) if pf_20  is not None else None,
            rolling_40          = round(pf_40,  4) if pf_40  is not None else None,
            pfs_score           = pfs_score,
            pfs_cv              = pfs_cv,
            pfs_label           = pfs_label,
            rolling_20_history  = [round(min(v, 10.0), 3) for v in pf_20_history],
            rolling_40_history  = [round(min(v, 10.0), 3) for v in pf_40_history],
        )

        # ── Drawdown in R ────────────────────────────────────────────────
        dd_r = _compute_drawdown_r(r_seq)

        # ── Cumulative R and rolling expectancy for charts ───────────────
        cumulative_r = []
        running = 0.0
        for r in r_seq:
            running += r
            cumulative_r.append(round(running, 4))

        rolling_20_exp_history = _compute_rolling_exp_series(r_seq, 20)

        # ── Dimension breakdowns ─────────────────────────────────────────
        exp_by_regime  = self._breakdown_by(sorted_trades, "regime")
        exp_by_asset   = self._breakdown_by(sorted_trades, "symbol")
        exp_by_model   = self._breakdown_by_model(sorted_trades)
        score_cal      = self._score_calibration(sorted_trades, t.score_bins)

        # ── Verdict / scoring ────────────────────────────────────────────
        checks_passed: list[str] = []
        checks_failed: list[str] = []
        verdict, score = self._determine_verdict(
            n=n, r_seq=r_seq, overall_exp=overall_exp,
            pf_metrics=pf_metrics, dd_r=dd_r,
            checks_passed=checks_passed, checks_failed=checks_failed,
        )

        explanation = self._build_explanation(
            verdict=verdict, n=n, r_seq=r_seq,
            overall_exp=overall_exp, pf_metrics=pf_metrics, dd_r=dd_r,
            checks_passed=checks_passed, checks_failed=checks_failed,
        )

        return EdgeAssessment(
            verdict               = verdict,
            score                 = score,
            trade_count           = n,
            overall_expectancy    = overall_exp,
            rolling_20_expectancy = rolling_20,
            rolling_40_expectancy = rolling_40,
            profit_factor_metrics = pf_metrics,
            drawdown_r            = dd_r,
            expectancy_by_regime  = exp_by_regime,
            expectancy_by_model   = exp_by_model,
            expectancy_by_asset   = exp_by_asset,
            score_calibration     = score_cal,
            cumulative_r_history  = cumulative_r,
            rolling_20_exp_history = rolling_20_exp_history,
            checks_passed         = checks_passed,
            checks_failed         = checks_failed,
            explanation           = explanation,
            generated_at          = _now_utc(),
        )

    # ── Dimension breakdown helpers ───────────────────────────────────────────

    def _breakdown_by(
        self, trades: list[dict], key: str
    ) -> dict[str, ExpectancyMetrics]:
        groups: dict[str, list[float]] = {}
        for tr in trades:
            dim = str(tr.get(key, "unknown") or "unknown").lower()
            r   = _r_from_dict(tr)
            if r is None:
                continue
            groups.setdefault(dim, []).append(r)
        return {
            dim: em for dim, rlist in groups.items()
            if (em := _compute_expectancy(rlist)) is not None
        }

    def _breakdown_by_model(
        self, trades: list[dict]
    ) -> dict[str, ExpectancyMetrics]:
        groups: dict[str, list[float]] = {}
        for tr in trades:
            r = _r_from_dict(tr)
            if r is None:
                continue
            for model in (tr.get("models_fired") or []):
                groups.setdefault(str(model), []).append(r)
        return {
            model: em for model, rlist in groups.items()
            if (em := _compute_expectancy(rlist)) is not None
        }

    def _score_calibration(
        self, trades: list[dict], bins: list
    ) -> dict[str, ScoreBucketMetrics]:
        """Score calibration is purely diagnostic — does not affect weights."""
        groups: dict[str, list[float]] = {}
        for tr in trades:
            score = float(tr.get("score", 0) or tr.get("confluence_score", 0) or 0)
            r     = _r_from_dict(tr)
            if r is None:
                continue
            for lo, hi in bins:
                if lo <= score < hi:
                    label = f"{lo:.2f}-{hi:.2f}".replace("1.01", "1.00")
                    groups.setdefault(label, []).append(r)
                    break
        result = {}
        for label, rlist in groups.items():
            wins   = [r for r in rlist if r > 0]
            losses = [r for r in rlist if r <= 0]
            wr     = len(wins) / len(rlist) if rlist else 0.0
            avg_r  = sum(rlist) / len(rlist)
            exp    = _compute_expectancy(rlist)
            result[label] = ScoreBucketMetrics(
                bucket       = label,
                trade_count  = len(rlist),
                win_rate     = round(wr, 4),
                avg_r        = round(avg_r, 4),
                expectancy_r = round(exp.expectancy_r, 4) if exp else 0.0,
            )
        return dict(sorted(result.items()))

    # ── Verdict logic ─────────────────────────────────────────────────────────

    def _determine_verdict(
        self,
        n:             int,
        r_seq:         list[float],
        overall_exp:   Optional[ExpectancyMetrics],
        pf_metrics:    ProfitFactorMetrics,
        dd_r:          float,
        checks_passed: list[str],
        checks_failed: list[str],
    ) -> tuple[str, int]:
        t = self._thresholds

        # ── Gather check results ─────────────────────────────────────────
        def chk(name: str, passed: bool) -> bool:
            (checks_passed if passed else checks_failed).append(name)
            return passed

        enough_trades_early = chk(
            f"Trade count ≥ {t.min_trades_early} (early threshold)",
            n >= t.min_trades_early,
        )
        enough_trades_full = chk(
            f"Trade count ≥ {t.min_trades_full} (full threshold)",
            n >= t.min_trades_full,
        )

        exp_r = overall_exp.expectancy_r if overall_exp else -999.0
        pf_v  = pf_metrics.overall if not math.isinf(pf_metrics.overall) else 999.0

        pos_expectancy = chk(
            "Expectancy > 0 R (any positive edge)",
            exp_r > 0,
        )
        early_exp = chk(
            f"Expectancy ≥ {t.min_expectancy_early}R (early threshold)",
            exp_r >= t.min_expectancy_early,
        )
        early_pf  = chk(
            f"Profit factor ≥ {t.min_pf_early} (early threshold)",
            pf_v >= t.min_pf_early,
        )
        full_exp  = chk(
            f"Expectancy ≥ {t.min_expectancy_full}R (full threshold)",
            exp_r >= t.min_expectancy_full,
        )
        full_pf   = chk(
            f"Profit factor ≥ {t.min_pf_full} (full threshold)",
            pf_v >= t.min_pf_full,
        )
        stable_pf = chk(
            f"Profit factor stability ≥ {t.min_pfs_score} (full threshold)",
            pf_metrics.pfs_score >= t.min_pfs_score,
        )
        dd_ok = chk(
            f"Drawdown in R < {t.max_drawdown_r}R",
            dd_r < t.max_drawdown_r,
        )

        # ── Weighted score (for NEEDS_IMPROVEMENT range) ──────────────────
        weights = [
            (early_exp,  3),
            (early_pf,   3),
            (full_exp,   3),
            (full_pf,    3),
            (stable_pf,  2),
            (dd_ok,      2),
            (pos_expectancy, 2),
        ]
        w_total = sum(w for _, w in weights)
        w_pass  = sum(w for passed, w in weights if passed)
        score   = int(round(w_pass / w_total * 100)) if w_total > 0 else 0

        # ── Verdict ───────────────────────────────────────────────────────
        if not enough_trades_early or not pos_expectancy or not dd_ok:
            verdict = EdgeVerdict.NOT_READY
        elif enough_trades_full and full_exp and full_pf and stable_pf and dd_ok:
            verdict = EdgeVerdict.READY_FOR_LIVE
        else:
            verdict = EdgeVerdict.NEEDS_IMPROVEMENT

        return verdict, score

    # ── Narrative ─────────────────────────────────────────────────────────────

    def _build_explanation(
        self,
        verdict:       str,
        n:             int,
        r_seq:         list[float],
        overall_exp:   Optional[ExpectancyMetrics],
        pf_metrics:    ProfitFactorMetrics,
        dd_r:          float,
        checks_passed: list[str],
        checks_failed: list[str],
    ) -> str:
        t = self._thresholds

        exp_r = overall_exp.expectancy_r if overall_exp else None
        pf_v  = pf_metrics.overall

        lines = []
        if verdict == EdgeVerdict.NOT_READY:
            if n < t.min_trades_early:
                lines.append(
                    f"NOT READY — only {n} completed trades "
                    f"({t.min_trades_early} required for early viability assessment). "
                    f"Insufficient data to judge edge quality."
                )
            elif exp_r is not None and exp_r <= 0:
                lines.append(
                    f"NOT READY — negative expectancy ({exp_r:+.3f}R). "
                    f"System is currently losing on average. "
                    f"Review IDSS signal quality and risk parameters."
                )
            else:
                lines.append(
                    f"NOT READY — {', '.join(checks_failed[:3])}."
                )

        elif verdict == EdgeVerdict.NEEDS_IMPROVEMENT:
            parts = []
            if exp_r is not None:
                parts.append(f"expectancy {exp_r:+.3f}R "
                             f"(target ≥ {t.min_expectancy_full}R)")
            parts.append(f"PF {pf_v:.2f} (target ≥ {t.min_pf_full})")
            parts.append(f"PF stability {pf_metrics.pfs_label} "
                         f"({pf_metrics.pfs_score:.0f}/100)")
            lines.append(
                f"NEEDS IMPROVEMENT (score {self._last_score(n, r_seq, overall_exp, pf_metrics, dd_r)}/100). "
                f"Current: {'; '.join(parts)}. "
                f"Failing checks: {', '.join(checks_failed[:4])}."
            )

        else:  # READY_FOR_LIVE
            lines.append(
                f"Edge appears READY FOR LIVE CAPITAL CONSIDERATION "
                f"({n} trades, expectancy {exp_r:+.3f}R, "
                f"PF {pf_v:.2f}, stability {pf_metrics.pfs_label}, "
                f"max drawdown {dd_r:.2f}R)."
            )
            if overall_exp:
                lines.append(
                    f"Win rate {overall_exp.win_rate*100:.1f}%, "
                    f"avg win {overall_exp.avg_win_r:.2f}R, "
                    f"avg loss −{overall_exp.avg_loss_r:.2f}R."
                )

        lines.append(
            "\n⚠  SAFETY NOTE: This is a recommendation only. "
            "NexusTrader cannot and will not automatically switch to live trading. "
            "The final decision requires manual user approval."
        )
        return "  ".join(lines)

    @staticmethod
    def _last_score(
        n: int,
        r_seq: list[float],
        overall_exp: Optional[ExpectancyMetrics],
        pf_metrics: ProfitFactorMetrics,
        dd_r: float,
    ) -> int:
        """Recompute score without side-effects for explanation string."""
        t = EdgeThresholds()
        exp_r = overall_exp.expectancy_r if overall_exp else -999.0
        pf_v  = pf_metrics.overall
        weights = [
            (exp_r >= t.min_expectancy_early,    3),
            (pf_v  >= t.min_pf_early,            3),
            (exp_r >= t.min_expectancy_full,      3),
            (pf_v  >= t.min_pf_full,             3),
            (pf_metrics.pfs_score >= t.min_pfs_score, 2),
            (dd_r  < t.max_drawdown_r,           2),
            (exp_r > 0,                          2),
        ]
        w_total = sum(w for _, w in weights)
        w_pass  = sum(w for p, w in weights if p)
        return int(round(w_pass / w_total * 100)) if w_total > 0 else 0

    @staticmethod
    def _explain_not_ready_empty() -> str:
        return (
            "NOT READY — no completed trades recorded. "
            "Begin demo trading to accumulate trade history for edge evaluation.  "
            "\n⚠  SAFETY NOTE: This is a recommendation only. "
            "NexusTrader cannot and will not automatically switch to live trading. "
            "The final decision requires manual user approval."
        )


# ── Utility ───────────────────────────────────────────────────────────────────

def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# ── Module singleton ──────────────────────────────────────────────────────────

_edge_evaluator = EdgeEvaluator()


def get_edge_evaluator() -> EdgeEvaluator:
    """Return the module-level EdgeEvaluator singleton."""
    return _edge_evaluator
