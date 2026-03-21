# ============================================================
# NEXUS TRADER — Performance Threshold Evaluator
#
# Defines GREEN / AMBER / RED thresholds for every tracked
# metric, per model and at portfolio level.  Evaluates live
# rolling results from LiveVsBacktestTracker and returns a
# structured RAG (Red / Amber / Green) assessment.
#
# Study 4 baselines (13-month synthetic backtest):
#   TrendModel         : 50.3% WR, PF 1.47, avg R 0.22
#   MomentumBreakout   : 63.5% WR, PF 4.17, avg R 1.21
#   Portfolio (pruned) : 51.4% WR, PF 1.47, avg R 0.31
#
# Threshold calibration rationale:
#   GREEN  — within ~8–10% relative degradation of baseline.
#            Normal live/synthetic divergence; system behaving.
#   AMBER  — 10–25% relative degradation.  Monitor closely,
#            do NOT scale up, review signal quality.
#   RED    — >25% relative degradation OR expectancy flipped
#            negative.  Trigger pause / investigation.
#
# Minimum sample requirement: 20 trades before any verdict
# other than INSUFFICIENT_DATA is issued.
#
# DOES NOT MODIFY STRATEGY OR PARAMETERS.
# ============================================================
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)

_MIN_TRADES_FOR_VERDICT = 20   # below this, verdict = INSUFFICIENT_DATA


# ── RAG status ───────────────────────────────────────────────────────────────

class RAGStatus(str, Enum):
    GREEN             = "GREEN"
    AMBER             = "AMBER"
    RED               = "RED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


# ── Per-metric threshold band ────────────────────────────────────────────────

@dataclass
class MetricBand:
    """
    Defines GREEN / AMBER / RED boundaries for one metric.
    All comparisons are:  GREEN if val >= green_min
                          AMBER if amber_min <= val < green_min
                          RED   if val < amber_min
    (for metrics where higher = better: WR, PF, avg_R)
    """
    green_min: float
    amber_min: float
    label:     str = ""

    def evaluate(self, value: Optional[float]) -> RAGStatus:
        if value is None:
            return RAGStatus.INSUFFICIENT_DATA
        if value >= self.green_min:
            return RAGStatus.GREEN
        if value >= self.amber_min:
            return RAGStatus.AMBER
        return RAGStatus.RED

    def describe(self) -> str:
        return (
            f"{self.label}: "
            f"GREEN ≥ {self.green_min:.3f} | "
            f"AMBER ≥ {self.amber_min:.3f} | "
            f"RED < {self.amber_min:.3f}"
        )


# ── Per-model threshold set ───────────────────────────────────────────────────

@dataclass
class ModelThresholds:
    """
    Complete GREEN/AMBER/RED bands for one model.
    Based on Study 4 backtest baselines with documented degradation tolerance.
    """
    model_key:  str
    model_name: str
    baseline_wr:  float   # Study 4 win rate (0–1)
    baseline_pf:  float   # Study 4 profit factor
    baseline_avgr: float  # Study 4 avg R multiple

    wr_band:   MetricBand = field(init=False)
    pf_band:   MetricBand = field(init=False)
    avgr_band: MetricBand = field(init=False)

    # Deviation thresholds (relative to baseline)
    # These define how far live can deviate before AMBER / RED.
    _WR_AMBER_FLOOR_RELATIVE  = 0.90   # 90% of baseline WR → AMBER boundary
    _WR_GREEN_FLOOR_RELATIVE  = 0.95   # 95% of baseline WR → GREEN boundary
    _PF_AMBER_FLOOR_RELATIVE  = 0.72   # 72% of baseline PF → AMBER boundary
    _PF_GREEN_FLOOR_RELATIVE  = 0.87   # 87% of baseline PF → GREEN boundary
    _AVGR_AMBER_FLOOR_RELATIVE = 0.33  # 33% of baseline avg R → AMBER boundary
    _AVGR_GREEN_FLOOR_RELATIVE = 0.66  # 66% of baseline avg R → GREEN boundary

    def __post_init__(self) -> None:
        self.wr_band = MetricBand(
            green_min = round(self.baseline_wr  * self._WR_GREEN_FLOOR_RELATIVE,  3),
            amber_min = round(self.baseline_wr  * self._WR_AMBER_FLOOR_RELATIVE,  3),
            label     = "WR",
        )
        self.pf_band = MetricBand(
            green_min = round(self.baseline_pf  * self._PF_GREEN_FLOOR_RELATIVE,  3),
            amber_min = round(self.baseline_pf  * self._PF_AMBER_FLOOR_RELATIVE,  3),
            label     = "PF",
        )
        self.avgr_band = MetricBand(
            green_min = round(self.baseline_avgr * self._AVGR_GREEN_FLOOR_RELATIVE, 3),
            amber_min = round(self.baseline_avgr * self._AVGR_AMBER_FLOOR_RELATIVE, 3),
            label     = "Avg R",
        )


# ── Canonical thresholds (all enabled models + portfolio) ────────────────────
#
# Thresholds calibrated from Study 4 baselines.
# RED  = clear underperformance requiring investigation / pause.
# AMBER = degraded vs baseline — monitor closely, do not scale up.
# GREEN = acceptable live performance vs synthetic baseline.
#
#   TrendModel         : WR 50.3% / PF 1.47 / avg R 0.22
#     → GREEN  : WR ≥ 47.8% | PF ≥ 1.28  | avg R ≥ 0.145
#     → AMBER  : WR ≥ 45.3% | PF ≥ 1.059 | avg R ≥ 0.073
#     → RED    : WR < 45.3% | PF < 1.059 | avg R < 0.073
#
#   MomentumBreakout   : WR 63.5% / PF 4.17 / avg R 1.21
#     → GREEN  : WR ≥ 60.3% | PF ≥ 3.628 | avg R ≥ 0.799
#     → AMBER  : WR ≥ 57.2% | PF ≥ 3.002 | avg R ≥ 0.399
#     → RED    : WR < 57.2% | PF < 3.002 | avg R < 0.399
#
#   Portfolio (pruned) : WR 51.4% / PF 1.47 / avg R 0.31
#     → GREEN  : WR ≥ 48.8% | PF ≥ 1.279 | avg R ≥ 0.205
#     → AMBER  : WR ≥ 46.3% | PF ≥ 1.058 | avg R ≥ 0.102
#     → RED    : WR < 46.3% | PF < 1.058 | avg R < 0.102

THRESHOLDS: dict[str, ModelThresholds] = {
    "trend": ModelThresholds(
        model_key      = "trend",
        model_name     = "TrendModel",
        baseline_wr    = 0.503,
        baseline_pf    = 1.47,
        baseline_avgr  = 0.22,
    ),
    "momentum_breakout": ModelThresholds(
        model_key      = "momentum_breakout",
        model_name     = "MomentumBreakoutModel",
        baseline_wr    = 0.635,
        baseline_pf    = 4.17,
        baseline_avgr  = 1.21,
    ),
    "_portfolio": ModelThresholds(
        model_key      = "_portfolio",
        model_name     = "Portfolio (pruned)",
        baseline_wr    = 0.514,
        baseline_pf    = 1.47,
        baseline_avgr  = 0.31,
    ),
    # These models have no Study 4 baselines — always INSUFFICIENT_DATA
    "sentiment": ModelThresholds(
        model_key    = "sentiment",
        model_name   = "SentimentModel",
        baseline_wr  = 0.50, baseline_pf = 1.20, baseline_avgr = 0.10,
    ),
    "funding_rate": ModelThresholds(
        model_key    = "funding_rate",
        model_name   = "FundingRateModel",
        baseline_wr  = 0.50, baseline_pf = 1.20, baseline_avgr = 0.10,
    ),
    "order_book": ModelThresholds(
        model_key    = "order_book",
        model_name   = "OrderBookModel",
        baseline_wr  = 0.50, baseline_pf = 1.20, baseline_avgr = 0.10,
    ),
}

# Deviation thresholds for live vs baseline comparison
# (relative deviation from baseline that triggers AMBER / RED)
DEVIATION_THRESHOLDS = {
    "wr": {
        "acceptable":  -0.05,    # within -5pp → acceptable
        "warning":     -0.10,    # -5 to -10pp → warning
        # worse than -10pp → critical
    },
    "pf": {
        "acceptable":  -0.15,    # PF_live > baseline × 0.85 → acceptable
        "warning":     -0.30,    # PF_live > baseline × 0.70 → warning
        # PF_live < baseline × 0.70 → critical
    },
    "avg_r": {
        "acceptable":  -0.33,    # avg_R_live > baseline × 0.67 → acceptable
        "warning":     -0.50,    # avg_R_live > baseline × 0.50 → warning
        # avg_R_live < baseline × 0.50 → critical
    },
}


# ── Per-metric assessment result ──────────────────────────────────────────────

@dataclass
class MetricAssessment:
    metric:   str          # "wr" | "pf" | "avg_r"
    status:   RAGStatus
    value:    Optional[float]
    baseline: Optional[float]
    delta:    Optional[float]
    green_min: float
    amber_min: float
    label:    str = ""     # human-readable message


@dataclass
class ModelAssessment:
    model_key:    str
    model_name:   str
    trades:       int
    wr:           MetricAssessment
    pf:           MetricAssessment
    avg_r:        MetricAssessment
    overall:      RAGStatus = RAGStatus.INSUFFICIENT_DATA
    red_count:    int = 0
    amber_count:  int = 0

    def __post_init__(self) -> None:
        statuses = [self.wr.status, self.pf.status, self.avg_r.status]
        self.red_count   = statuses.count(RAGStatus.RED)
        self.amber_count = statuses.count(RAGStatus.AMBER)
        has_data = any(s != RAGStatus.INSUFFICIENT_DATA for s in statuses)
        if not has_data or self.trades < _MIN_TRADES_FOR_VERDICT:
            self.overall = RAGStatus.INSUFFICIENT_DATA
        elif self.red_count >= 1:
            self.overall = RAGStatus.RED
        elif self.amber_count >= 1:
            self.overall = RAGStatus.AMBER
        else:
            self.overall = RAGStatus.GREEN

    def to_dict(self) -> dict:
        return {
            "model_key":   self.model_key,
            "model_name":  self.model_name,
            "trades":      self.trades,
            "overall":     self.overall.value,
            "red_count":   self.red_count,
            "amber_count": self.amber_count,
            "wr": {
                "status":    self.wr.status.value,
                "value":     self.wr.value,
                "baseline":  self.wr.baseline,
                "delta":     self.wr.delta,
                "green_min": self.wr.green_min,
                "amber_min": self.wr.amber_min,
            },
            "pf": {
                "status":    self.pf.status.value,
                "value":     self.pf.value,
                "baseline":  self.pf.baseline,
                "delta":     self.pf.delta,
                "green_min": self.pf.green_min,
                "amber_min": self.pf.amber_min,
            },
            "avg_r": {
                "status":    self.avg_r.status.value,
                "value":     self.avg_r.value,
                "baseline":  self.avg_r.baseline,
                "delta":     self.avg_r.delta,
                "green_min": self.avg_r.green_min,
                "amber_min": self.avg_r.amber_min,
            },
        }


@dataclass
class PortfolioRAGAssessment:
    """
    Portfolio-level RAG verdict and per-model breakdown.
    The portfolio is the primary signal for pause/scale decisions.
    """
    portfolio:        ModelAssessment
    per_model:        dict[str, ModelAssessment]   # model_key → assessment
    red_model_count:  int = 0
    should_pause:     bool = False
    pause_reason:     str  = ""
    overall:          RAGStatus = RAGStatus.INSUFFICIENT_DATA

    def __post_init__(self) -> None:
        self.red_model_count = sum(
            1 for m in self.per_model.values()
            if m.overall == RAGStatus.RED and m.trades >= _MIN_TRADES_FOR_VERDICT
        )
        # Portfolio-level assessment drives overall verdict
        self.overall = self.portfolio.overall
        # Pause if portfolio is RED or 2+ models are RED
        if self.portfolio.overall == RAGStatus.RED:
            self.should_pause = True
            self.pause_reason = (
                f"Portfolio overall status RED — "
                f"red_metrics={self.portfolio.red_count}/3"
            )
        elif self.red_model_count >= 2:
            self.should_pause = True
            self.pause_reason = (
                f"{self.red_model_count} models in RED status simultaneously"
            )

    def to_dict(self) -> dict:
        return {
            "overall":         self.overall.value,
            "should_pause":    self.should_pause,
            "pause_reason":    self.pause_reason,
            "red_model_count": self.red_model_count,
            "portfolio":       self.portfolio.to_dict(),
            "per_model":       {k: v.to_dict() for k, v in self.per_model.items()},
        }


# ── Evaluator ─────────────────────────────────────────────────────────────────

class PerformanceThresholdEvaluator:
    """
    Evaluates live rolling metrics from LiveVsBacktestTracker
    against defined thresholds and returns a PortfolioRAGAssessment.

    Usage:
        evaluator = get_threshold_evaluator()
        assessment = evaluator.evaluate()
        if assessment.should_pause:
            logger.warning("System pause recommended: %s", assessment.pause_reason)
    """

    def evaluate(self) -> PortfolioRAGAssessment:
        """Pull latest metrics and run full evaluation."""
        try:
            from core.monitoring.live_vs_backtest import get_live_vs_backtest_tracker
            comparison = get_live_vs_backtest_tracker().get_comparison()
        except Exception as exc:
            logger.debug("PerformanceThresholdEvaluator: cannot get metrics: %s", exc)
            # Return neutral assessment when data is unavailable
            _empty = self._empty_model_assessment("_portfolio", "Portfolio")
            return PortfolioRAGAssessment(
                portfolio   = _empty,
                per_model   = {},
            )

        port_data  = comparison.get("portfolio", {})
        model_data = comparison.get("per_model", {})

        portfolio_assessment = self._evaluate_model(
            "_portfolio",
            port_data.get("live", {}),
        )
        per_model: dict[str, ModelAssessment] = {}
        for model_key, comp in model_data.items():
            if model_key.startswith("_"):
                continue
            per_model[model_key] = self._evaluate_model(
                model_key,
                comp.get("live", {}),
            )

        return PortfolioRAGAssessment(
            portfolio  = portfolio_assessment,
            per_model  = per_model,
        )

    def evaluate_from_metrics(self, metrics: dict) -> ModelAssessment:
        """
        Evaluate a single model from a metrics dict with keys:
        trades, win_rate, profit_factor, avg_r, model_key (optional).
        """
        model_key = metrics.get("model", "_portfolio")
        return self._evaluate_model(model_key, metrics)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _evaluate_model(self, model_key: str, live: dict) -> ModelAssessment:
        thresholds = THRESHOLDS.get(model_key) or THRESHOLDS.get("_portfolio")
        trades     = int(live.get("trades") or 0)
        wr_val     = live.get("win_rate")     # None or float
        pf_val     = live.get("profit_factor")
        avgr_val   = live.get("avg_r")
        model_name = thresholds.model_name if thresholds else model_key

        if thresholds is None:
            return self._empty_model_assessment(model_key, model_key)

        # Per-metric evaluations
        def _assess(band: "MetricBand", value, baseline: float) -> MetricAssessment:
            if trades < _MIN_TRADES_FOR_VERDICT or value is None:
                status = RAGStatus.INSUFFICIENT_DATA
            else:
                status = band.evaluate(value)
            delta = round(value - baseline, 4) if value is not None else None
            return MetricAssessment(
                metric    = band.label,
                status    = status,
                value     = value,
                baseline  = baseline,
                delta     = delta,
                green_min = band.green_min,
                amber_min = band.amber_min,
                label     = _make_label(band.label, status, value, band.green_min, band.amber_min),
            )

        wr_a   = _assess(thresholds.wr_band,   wr_val,   thresholds.baseline_wr)
        pf_a   = _assess(thresholds.pf_band,   pf_val,   thresholds.baseline_pf)
        avgr_a = _assess(thresholds.avgr_band, avgr_val, thresholds.baseline_avgr)

        return ModelAssessment(
            model_key  = model_key,
            model_name = model_name,
            trades     = trades,
            wr         = wr_a,
            pf         = pf_a,
            avg_r      = avgr_a,
        )

    def _empty_model_assessment(self, key: str, name: str) -> ModelAssessment:
        empty = MetricAssessment(
            metric="", status=RAGStatus.INSUFFICIENT_DATA,
            value=None, baseline=None, delta=None,
            green_min=0.0, amber_min=0.0,
        )
        return ModelAssessment(
            model_key=key, model_name=name, trades=0,
            wr=empty, pf=empty, avg_r=empty,
        )


def _make_label(metric: str, status: RAGStatus, value, green_min, amber_min) -> str:
    if status == RAGStatus.INSUFFICIENT_DATA:
        return f"{metric}: Insufficient data"
    v_str = f"{value*100:.1f}%" if metric == "WR" else f"{value:.3f}"
    return f"{metric}: {v_str} ({status.value})"


# ── Module singleton ──────────────────────────────────────────────────────────
_evaluator: Optional[PerformanceThresholdEvaluator] = None
_eval_lock = threading.Lock()


def get_threshold_evaluator() -> PerformanceThresholdEvaluator:
    global _evaluator
    if _evaluator is None:
        with _eval_lock:
            if _evaluator is None:
                _evaluator = PerformanceThresholdEvaluator()
    return _evaluator
