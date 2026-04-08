# ============================================================
# NEXUS TRADER — Demo Performance Evaluator
#
# Analyzes completed paper-trading results and determines
# whether NexusTrader is ready for live capital deployment.
#
# SAFETY CONTRACT
# ══════════════════════════════════════════════════════════
# This module ONLY monitors, evaluates, and recommends.
# It NEVER modifies the OrderRouter mode, triggers live
# execution, or changes any system settings.
# The final switch to live trading requires manual user
# approval — this class cannot perform that switch.
# ══════════════════════════════════════════════════════════
from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ── Readiness status constants ───────────────────────────────────────────────
class ReadinessStatus:
    NOT_READY          = "NOT_READY"
    NEEDS_IMPROVEMENT  = "NEEDS_IMPROVEMENT"
    READY_FOR_LIVE     = "READY_FOR_LIVE"


# ── Per-check result ─────────────────────────────────────────────────────────
@dataclass
class CheckResult:
    name:        str
    passed:      bool
    actual:      str          # human-readable actual value
    threshold:   str          # human-readable threshold
    weight:      int          # importance 1–3 (3 = blocking)
    notes:       str = ""


# ── Full readiness assessment ────────────────────────────────────────────────
@dataclass
class ReadinessAssessment:
    status:         str                    # ReadinessStatus constant
    score:          int                    # 0–100
    checks_passed:  int
    checks_total:   int
    explanation:    str                    # narrative summary
    check_details:  list[CheckResult]      = field(default_factory=list)
    generated_at:   str                    = ""
    trade_count:    int                    = 0
    # Computed metric snapshot used by dashboard
    metrics:        dict                   = field(default_factory=dict)
    # Edge evaluation (EdgeEvaluator verdict — separate from readiness score)
    edge_assessment: object                = None   # EdgeAssessment | None


# ── Thresholds ───────────────────────────────────────────────────────────────
@dataclass
class ReadinessThresholds:
    # Sample size
    min_trades:             int   = 75

    # Profitability
    min_win_rate_pct:       float = 45.0
    min_profit_factor:      float = 1.25
    min_avg_rr:             float = 1.0   # average realized reward:risk

    # Risk control
    max_drawdown_pct:       float = 15.0
    max_rolling_dd_pct:     float = 10.0  # any 20-trade rolling window
    max_avg_heat_pct:       float = 5.0   # average portfolio heat

    # Coverage
    min_regimes_covered:    int   = 3     # distinct regime labels
    min_assets_covered:     int   = 2     # distinct symbols

    # Diversity (concentration limits)
    max_asset_concentration: float = 0.80   # one asset ≤ 80% of trades
    max_model_concentration: float = 0.80   # one model ≤ 80% of trades

    # Execution realism
    max_avg_slippage_pct:   float = 0.30  # avg |entry fill - expected| / entry

    # Stability
    min_span_days:          float = 2.0   # at least N calendar days of history

    # Learning (only checked when TradeOutcomeTracker has data)
    min_learning_models:    int   = 2     # models with ≥5 outcomes recorded

    # Condition diversity (new — checks 17–21)
    min_condition_pairs:    int   = 3     # distinct (model, regime) pairs with ≥5 trades
    max_regime_loss_rate:   float = 0.70  # regime with ≥10 trades must not exceed 70% loss rate
    max_consecutive_losses: int   = 8     # hard limit — system is too streaky if exceeded
    min_rl_shadow_trades:   int   = 30    # RL shadow check only applies after this many entries


# ── Main evaluator ───────────────────────────────────────────────────────────
class DemoPerformanceEvaluator:
    """
    Periodically evaluates demo trading results and produces a structured
    readiness assessment.  Thread-safe; can be called from any thread.

    Usage
    ─────
    evaluator = DemoPerformanceEvaluator()
    assessment = evaluator.evaluate()
    print(assessment.status)          # "NOT_READY" | "NEEDS_IMPROVEMENT" | "READY_FOR_LIVE"
    print(assessment.explanation)

    SAFETY: This class NEVER switches trading modes automatically.
    """

    def __init__(self, thresholds: Optional[ReadinessThresholds] = None):
        self._thresholds = thresholds or ReadinessThresholds()
        self._lock       = threading.Lock()
        self._last:      Optional[ReadinessAssessment] = None

    # ── Public API ───────────────────────────────────────────────────────────

    def evaluate(self, closed_trades: Optional[list[dict]] = None) -> ReadinessAssessment:
        """
        Run all readiness checks and return a ReadinessAssessment.

        If closed_trades is None, fetches from the active executor via order_router
        automatically.

        Also runs the EdgeEvaluator and attaches the EdgeAssessment as
        assessment.edge_assessment.  The two evaluations are independent.
        """
        with self._lock:
            if closed_trades is None:
                closed_trades = self._fetch_trades()

            checks, metrics = self._run_checks(closed_trades)
            assessment = self._build_assessment(checks, metrics, len(closed_trades))

            # Run edge evaluation (non-blocking — failure doesn't affect readiness)
            try:
                from core.evaluation.edge_evaluator import get_edge_evaluator
                assessment.edge_assessment = get_edge_evaluator().evaluate(closed_trades)
            except Exception as exc:
                logger.warning("DemoPerformanceEvaluator: edge evaluation failed — %s", exc)

            self._last = assessment
            return assessment

    def last_assessment(self) -> Optional[ReadinessAssessment]:
        """Return the most recent assessment without re-evaluating."""
        with self._lock:
            return self._last

    @property
    def thresholds(self) -> ReadinessThresholds:
        return self._thresholds

    # ── Internal ─────────────────────────────────────────────────────────────

    @staticmethod
    def _fetch_trades() -> list[dict]:
        try:
            from core.execution.order_router import order_router
            return order_router.active_executor.get_closed_trades()
        except Exception as exc:
            logger.warning("DemoPerformanceEvaluator: could not fetch trades — %s", exc)
            return []

    def _run_checks(
        self, trades: list[dict]
    ) -> tuple[list[CheckResult], dict]:
        """Run all checks and return (check_list, metrics_dict)."""
        t   = self._thresholds
        n   = len(trades)
        chk: list[CheckResult] = []
        met: dict              = {"trade_count": n}

        # ── 1 — Sample size ──────────────────────────────────────────────
        chk.append(CheckResult(
            name      = "Minimum trade count",
            passed    = n >= t.min_trades,
            actual    = str(n),
            threshold = f"≥ {t.min_trades}",
            weight    = 3,
            notes     = "Insufficient evidence if below threshold.",
        ))

        if n == 0:
            # All subsequent checks are meaningless without any trades
            return chk, met

        # ── Precompute shared values ─────────────────────────────────────
        # Use (x or 0.0) to guard against rare None values from DB rows
        # that predate a NOT NULL migration or were written by test runs.
        pnl_list   = [(tr.get("pnl_usdt")  or 0.0) for tr in trades]
        pnl_pct_l  = [(tr.get("pnl_pct")   or 0.0) for tr in trades]
        wins       = [tr for tr in trades if (tr.get("pnl_pct") or 0) > 0]
        losses     = [tr for tr in trades if (tr.get("pnl_pct") or 0) <= 0]
        win_rate   = len(wins) / n * 100

        gross_win  = sum(p for p in pnl_list if p > 0)
        gross_loss = abs(sum(p for p in pnl_list if p < 0))
        pf         = round(gross_win / gross_loss, 4) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)

        total_pnl  = sum(pnl_list)

        # Average realized R:R
        rr_list    = self._compute_rr_list(trades)
        avg_rr     = sum(rr_list) / len(rr_list) if rr_list else 0.0

        # Drawdown (running equity)
        max_dd, rolling_dd = self._compute_drawdown(trades)

        # Regime and asset coverage
        regimes  = {(tr.get("regime") or "unknown").lower() for tr in trades}
        assets   = {tr.get("symbol", "?") for tr in trades}
        sides    = {tr.get("side", "?") for tr in trades}

        # Asset / model concentration
        asset_counts = {}
        model_counts = {}
        for tr in trades:
            sym = tr.get("symbol", "?")
            asset_counts[sym] = asset_counts.get(sym, 0) + 1
            for m in tr.get("models_fired") or []:
                model_counts[m] = model_counts.get(m, 0) + 1
        top_asset_pct = max(asset_counts.values()) / n if asset_counts else 0.0
        top_model_pct = max(model_counts.values()) / n if model_counts else 0.0

        # Execution realism: avg slippage
        avg_slip   = self._compute_avg_slippage(trades)

        # Calendar span
        span_days  = self._compute_span_days(trades)

        # Expected vs realized EV
        avg_ev_gap = self._compute_ev_gap(trades)

        # Trading activity rate
        trades_per_day = (n / span_days) if span_days > 0 else n

        # Learning loop status
        learning_models, learning_summary = self._check_learning_status()

        # Record metrics snapshot
        met.update({
            "win_rate_pct":       round(win_rate, 2),
            "loss_rate_pct":      round(100 - win_rate, 2),
            "profit_factor":      round(pf, 3) if not math.isinf(pf) else 999.0,
            "total_pnl_usdt":     round(total_pnl, 2),
            "gross_win_usdt":     round(gross_win, 2),
            "gross_loss_usdt":    round(gross_loss, 2),
            "avg_rr":             round(avg_rr, 3),
            "max_drawdown_pct":   round(max_dd, 3),
            "rolling_dd_pct":     round(rolling_dd, 3),
            "regimes_covered":    sorted(regimes),
            "assets_covered":     sorted(assets),
            "sides_covered":      sorted(sides),
            "top_asset_pct":      round(top_asset_pct * 100, 1),
            "top_model_pct":      round(top_model_pct * 100, 1),
            "avg_slippage_pct":   round(avg_slip, 4),
            "span_days":          round(span_days, 1),
            "trades_per_day":     round(trades_per_day, 2),
            "learning_models":    learning_models,
            "learning_summary":   learning_summary,
            "asset_breakdown":    {k: v for k, v in sorted(asset_counts.items())},
            "model_breakdown":    {k: v for k, v in sorted(model_counts.items())},
            "avg_ev_gap":         round(avg_ev_gap, 4),
        })

        # ── 2 — Win rate ─────────────────────────────────────────────────
        chk.append(CheckResult(
            name      = "Win rate",
            passed    = win_rate >= t.min_win_rate_pct,
            actual    = f"{win_rate:.1f}%",
            threshold = f"≥ {t.min_win_rate_pct}%",
            weight    = 2,
        ))

        # ── 3 — Profit factor ────────────────────────────────────────────
        pf_display = f"{pf:.2f}" if not math.isinf(pf) else "∞"
        chk.append(CheckResult(
            name      = "Profit factor",
            passed    = pf >= t.min_profit_factor,
            actual    = pf_display,
            threshold = f"≥ {t.min_profit_factor}",
            weight    = 2,
        ))

        # ── 4 — Positive total P&L ───────────────────────────────────────
        chk.append(CheckResult(
            name      = "Positive total P&L",
            passed    = total_pnl > 0,
            actual    = f"${total_pnl:+.2f}",
            threshold = "> $0.00",
            weight    = 2,
        ))

        # ── 5 — Average R:R ──────────────────────────────────────────────
        chk.append(CheckResult(
            name      = "Average reward:risk ratio",
            passed    = avg_rr >= t.min_avg_rr,
            actual    = f"{avg_rr:.2f}",
            threshold = f"≥ {t.min_avg_rr:.1f}",
            weight    = 2,
        ))

        # ── 6 — Max drawdown ─────────────────────────────────────────────
        chk.append(CheckResult(
            name      = "Maximum drawdown",
            passed    = max_dd < t.max_drawdown_pct,
            actual    = f"{max_dd:.2f}%",
            threshold = f"< {t.max_drawdown_pct}%",
            weight    = 3,
        ))

        # ── 7 — Rolling drawdown ─────────────────────────────────────────
        chk.append(CheckResult(
            name      = "Rolling drawdown (worst 20-trade window)",
            passed    = rolling_dd < t.max_rolling_dd_pct,
            actual    = f"{rolling_dd:.2f}%",
            threshold = f"< {t.max_rolling_dd_pct}%",
            weight    = 2,
        ))

        # ── 8 — Regime coverage ──────────────────────────────────────────
        n_regimes = len(regimes)
        chk.append(CheckResult(
            name      = "Market regime coverage",
            passed    = n_regimes >= t.min_regimes_covered,
            actual    = f"{n_regimes} regime(s): {', '.join(sorted(regimes)[:4])}",
            threshold = f"≥ {t.min_regimes_covered} distinct regimes",
            weight    = 2,
            notes     = "Must include bull, bear, ranging at minimum.",
        ))

        # ── 9 — Asset coverage ───────────────────────────────────────────
        n_assets = len(assets)
        chk.append(CheckResult(
            name      = "Asset coverage",
            passed    = n_assets >= t.min_assets_covered,
            actual    = f"{n_assets} asset(s): {', '.join(sorted(assets)[:5])}",
            threshold = f"≥ {t.min_assets_covered} distinct assets",
            weight    = 1,
        ))

        # ── 10 — Asset concentration ─────────────────────────────────────
        top_a = max(asset_counts, key=asset_counts.get, default="—") if asset_counts else "—"
        chk.append(CheckResult(
            name      = "Asset concentration",
            passed    = top_asset_pct <= t.max_asset_concentration,
            actual    = f"{top_asset_pct*100:.0f}% in {top_a}",
            threshold = f"≤ {t.max_asset_concentration*100:.0f}% in any one asset",
            weight    = 1,
        ))

        # ── 11 — Model concentration ─────────────────────────────────────
        top_m = max(model_counts, key=model_counts.get, default="—") if model_counts else "—"
        chk.append(CheckResult(
            name      = "Model concentration",
            passed    = top_model_pct <= t.max_model_concentration,
            actual    = f"{top_model_pct*100:.0f}% from {top_m}",
            threshold = f"≤ {t.max_model_concentration*100:.0f}% from any one model",
            weight    = 1,
        ))

        # ── 12 — Execution realism (slippage) ────────────────────────────
        slip_note = "(no entry_expected data available)" if avg_slip == 0.0 else ""
        chk.append(CheckResult(
            name      = "Average slippage",
            passed    = avg_slip <= t.max_avg_slippage_pct,
            actual    = f"{avg_slip:.4f}%",
            threshold = f"≤ {t.max_avg_slippage_pct}%",
            weight    = 1,
            notes     = slip_note,
        ))

        # ── 13 — Calendar span ───────────────────────────────────────────
        chk.append(CheckResult(
            name      = "Demo trading history span",
            passed    = span_days >= t.min_span_days,
            actual    = f"{span_days:.1f} days",
            threshold = f"≥ {t.min_span_days:.0f} calendar days",
            weight    = 2,
            notes     = "Short history may reflect luck rather than systematic edge.",
        ))

        # ── 14 — Long and short coverage ─────────────────────────────────
        both_sides = "buy" in sides and "sell" in sides
        chk.append(CheckResult(
            name      = "Long and short trades present",
            passed    = both_sides,
            actual    = f"Sides observed: {', '.join(sorted(sides))}",
            threshold = "Both buy and sell",
            weight    = 1,
            notes     = "System should demonstrate bidirectional capability.",
        ))

        # ── 15 — Learning loop activity (Level 1) ────────────────────────
        chk.append(CheckResult(
            name      = "Adaptive learning loop activity",
            passed    = learning_models >= t.min_learning_models,
            actual    = f"{learning_models} model(s) with ≥5 outcomes recorded",
            threshold = f"≥ {t.min_learning_models} model(s)",
            weight    = 1,
            notes     = learning_summary,
        ))

        # ── 16 — Level-2 contextual learning cells active ────────────────
        l2_active, l2_total, l2_notes = self._check_l2_status()
        chk.append(CheckResult(
            name      = "Level-2 contextual learning",
            passed    = l2_active >= 1,
            actual    = f"{l2_active}/{l2_total} cells active (regime+asset × model)",
            threshold = "≥ 1 active cell",
            weight    = 1,
            notes     = l2_notes,
        ))

        # ── 17 — Condition diversity ───────────────────────────────────
        # Tracks distinct (model, regime) pairs with ≥5 trades.
        # Prevents the evaluator from passing on 75 trades of the same signal.
        cond_pairs = self._compute_condition_diversity(trades)
        met["condition_pairs"] = cond_pairs["qualified_pairs"]
        met["condition_details"] = cond_pairs["details"]
        chk.append(CheckResult(
            name      = "Condition diversity",
            passed    = cond_pairs["qualified_count"] >= t.min_condition_pairs,
            actual    = f"{cond_pairs['qualified_count']} (model×regime) pair(s) with ≥5 trades",
            threshold = f"≥ {t.min_condition_pairs} distinct pairs",
            weight    = 2,
            notes     = f"Pairs: {', '.join(cond_pairs['qualified_pairs'][:6]) or 'none'}",
        ))

        # ── 18 — Regime-specific win rate ──────────────────────────────
        # No regime with ≥10 trades should have >70% loss rate.
        regime_wr = self._compute_regime_win_rates(trades)
        met["regime_win_rates"] = regime_wr["breakdown"]
        worst_regime = regime_wr.get("worst_regime", "—")
        worst_lr     = regime_wr.get("worst_loss_rate", 0.0)
        regime_ok    = not regime_wr.get("has_failing_regime", False)
        chk.append(CheckResult(
            name      = "Regime-specific win rate",
            passed    = regime_ok,
            actual    = f"Worst: {worst_regime} ({worst_lr:.0f}% loss rate)" if worst_regime != "—" else "Insufficient data",
            threshold = f"No regime with ≥10 trades above {t.max_regime_loss_rate*100:.0f}% loss rate",
            weight    = 2,
            notes     = "Catches regime-dependent overfitting.",
        ))

        # ── 19 — Max consecutive losses ────────────────────────────────
        streak_data = self._compute_streaks(trades)
        met["max_consecutive_losses"] = streak_data["max_loss_streak"]
        met["max_consecutive_wins"]   = streak_data["max_win_streak"]
        chk.append(CheckResult(
            name      = "Max consecutive losses",
            passed    = streak_data["max_loss_streak"] <= t.max_consecutive_losses,
            actual    = f"{streak_data['max_loss_streak']} consecutive losses",
            threshold = f"≤ {t.max_consecutive_losses}",
            weight    = 2,
            notes     = f"Max win streak: {streak_data['max_win_streak']}",
        ))

        # ── 20 — RL shadow comparison ──────────────────────────────────
        # Compares RL's hypothetical recommendations against actual outcomes.
        rl_shadow = self._check_rl_shadow()
        met["rl_shadow"] = rl_shadow
        rl_sufficient = rl_shadow["total"] >= t.min_rl_shadow_trades
        rl_note = (
            f"RL aligned WR: {rl_shadow['aligned_wr']:.0f}%, "
            f"Misaligned WR: {rl_shadow['misaligned_wr']:.0f}%, "
            f"RL boost: {rl_shadow['rl_boost']:+.0f}pp"
            if rl_sufficient else
            f"Only {rl_shadow['total']} shadow entries (need {t.min_rl_shadow_trades})"
        )
        # This is informational (weight 1), not blocking
        chk.append(CheckResult(
            name      = "RL shadow performance",
            passed    = True,  # always passes — purely diagnostic
            actual    = rl_note,
            threshold = "Informational (diagnostic only)",
            weight    = 1,
            notes     = "Re-enable RL if aligned WR > baseline WR by ≥5pp.",
        ))

        return chk, met

    # ── Scoring and narrative ─────────────────────────────────────────────────

    def _build_assessment(
        self,
        checks:      list[CheckResult],
        metrics:     dict,
        trade_count: int,
    ) -> ReadinessAssessment:
        t            = self._thresholds
        n            = trade_count
        passed       = sum(1 for c in checks if c.passed)
        total        = len(checks)
        blocking_fail = any(c.weight == 3 and not c.passed for c in checks)

        # Weighted score: weight-3 checks count 3×, weight-2 count 2×, weight-1 count 1×
        w_sum    = sum(c.weight for c in checks)
        w_pass   = sum(c.weight for c in checks if c.passed)
        score    = int(round(w_pass / w_sum * 100)) if w_sum > 0 else 0

        # Status determination
        if n < t.min_trades or blocking_fail:
            status = ReadinessStatus.NOT_READY
        elif score >= 80 and not blocking_fail:
            status = ReadinessStatus.READY_FOR_LIVE
        else:
            status = ReadinessStatus.NEEDS_IMPROVEMENT

        explanation = self._build_explanation(status, checks, metrics, n, score)

        return ReadinessAssessment(
            status        = status,
            score         = score,
            checks_passed = passed,
            checks_total  = total,
            explanation   = explanation,
            check_details = checks,
            generated_at  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            trade_count   = n,
            metrics       = metrics,
        )

    def _build_explanation(
        self,
        status:  str,
        checks:  list[CheckResult],
        metrics: dict,
        n:       int,
        score:   int,
    ) -> str:
        t = self._thresholds
        lines = []

        if status == ReadinessStatus.NOT_READY:
            if n < t.min_trades:
                lines.append(
                    f"System is NOT ready — only {n} demo trades have completed "
                    f"({t.min_trades} required). Insufficient evidence to evaluate "
                    f"strategy quality or risk behavior."
                )
            else:
                blocking = [c for c in checks if c.weight == 3 and not c.passed]
                reasons  = "; ".join(
                    f"{c.name} is {c.actual} (threshold {c.threshold})"
                    for c in blocking
                )
                lines.append(
                    f"System is NOT ready. Blocking issue(s): {reasons}."
                )

        elif status == ReadinessStatus.NEEDS_IMPROVEMENT:
            failing = [c for c in checks if not c.passed]
            details = "; ".join(
                f"{c.name} ({c.actual} vs threshold {c.threshold})"
                for c in failing[:4]
            )
            lines.append(
                f"System NEEDS IMPROVEMENT before live trading (score {score}/100). "
                f"Failing: {details}."
            )

        else:  # READY_FOR_LIVE
            wr   = metrics.get("win_rate_pct", 0)
            pf   = metrics.get("profit_factor", 0)
            dd   = metrics.get("max_drawdown_pct", 0)
            rr   = metrics.get("avg_rr", 0)
            slip = metrics.get("avg_slippage_pct", 0)
            reg  = len(metrics.get("regimes_covered", []))
            lm   = metrics.get("learning_models", 0)

            lines.append(
                f"System appears READY FOR LIVE CAPITAL CONSIDERATION "
                f"(score {score}/100, {n} demo trades completed)."
            )
            lines.append(
                f"Evidence: win rate {wr:.1f}%, profit factor {pf:.2f}, "
                f"max drawdown {dd:.2f}%, avg R:R {rr:.2f}, "
                f"avg slippage {slip:.3f}%, {reg} regime(s) covered."
            )
            if lm > 0:
                lines.append(
                    f"Adaptive learning loop active: {lm} model(s) have accumulated "
                    f"≥5 trade outcomes and are adjusting weights dynamically."
                )

        lines.append(
            "\n⚠  SAFETY NOTE: This is a recommendation only. "
            "NexusTrader cannot and will not automatically switch to live trading. "
            "The final decision requires manual user approval."
        )
        return "  ".join(lines)

    # ── Metric helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _compute_rr_list(trades: list[dict]) -> list[float]:
        """
        Compute realized R:R for each trade.
        R:R = (take_profit - entry) / (entry - stop_loss) for longs.
        We use the configured SL/TP on the trade dict to reflect intended R:R.
        """
        rr_list = []
        for tr in trades:
            entry = float(tr.get("entry_price") or 0.0)
            sl    = float(tr.get("stop_loss")   or 0.0)
            tp    = float(tr.get("take_profit") or 0.0)
            side  = tr.get("side", "buy")
            if entry <= 0 or sl <= 0 or tp <= 0:
                continue
            if side == "buy":
                risk   = entry - sl
                reward = tp - entry
            else:
                risk   = sl - entry
                reward = entry - tp
            if risk > 0:
                rr_list.append(reward / risk)
        return rr_list

    @staticmethod
    def _compute_drawdown(trades: list[dict]) -> tuple[float, float]:
        """
        Returns (max_drawdown_pct, worst_rolling_dd_pct).
        Rolling window = 20 trades.
        """
        if not trades:
            return 0.0, 0.0

        sorted_t = sorted(trades, key=lambda t: t.get("closed_at", ""))
        pnl_seq  = [(t.get("pnl_usdt") or 0.0) for t in sorted_t]

        # Compute running equity (assume base=0 for relative drawdown)
        equity   = [0.0]
        for p in pnl_seq:
            equity.append(equity[-1] + p)

        peak     = equity[0]
        max_dd   = 0.0
        for val in equity[1:]:
            if val > peak:
                peak = val
            if peak > 0:
                dd = (peak - val) / peak * 100
                max_dd = max(max_dd, dd)

        # Rolling 20-trade window
        window   = 20
        roll_dd  = 0.0
        for i in range(len(pnl_seq) - window + 1):
            chunk = pnl_seq[i : i + window]
            sub   = [0.0]
            for p in chunk:
                sub.append(sub[-1] + p)
            spk   = sub[0]
            for val in sub[1:]:
                if val > spk:
                    spk = val
                if spk > 0:
                    dd = (spk - val) / spk * 100
                    roll_dd = max(roll_dd, dd)

        return max_dd, roll_dd

    @staticmethod
    def _compute_avg_slippage(trades: list[dict]) -> float:
        """
        Slippage: |entry_price - entry_expected| / entry_expected * 100.
        Falls back to 0.0 if entry_expected is not stored in trade dict.
        """
        slips = []
        for tr in trades:
            entry    = float(tr.get("entry_price")    or 0.0)
            expected = float(tr.get("entry_expected") or 0.0)
            if entry > 0 and expected > 0:
                slips.append(abs(entry - expected) / expected * 100)
        return sum(slips) / len(slips) if slips else 0.0

    @staticmethod
    def _compute_span_days(trades: list[dict]) -> float:
        """Calendar span of demo history in days."""
        if not trades:
            return 0.0
        try:
            ts_list = []
            for tr in trades:
                for key in ("opened_at", "closed_at"):
                    raw = tr.get(key)
                    if raw:
                        if isinstance(raw, str):
                            ts_list.append(datetime.fromisoformat(raw).timestamp())
                        elif isinstance(raw, datetime):
                            ts_list.append(raw.timestamp())
            if len(ts_list) < 2:
                return 0.0
            return (max(ts_list) - min(ts_list)) / 86400
        except Exception:
            return 0.0

    @staticmethod
    def _compute_ev_gap(trades: list[dict]) -> float:
        """Average difference between expected EV (score proxy) and realized PnL%."""
        gaps = []
        for tr in trades:
            expected_ev = tr.get("expected_value", None)
            actual_pct  = tr.get("pnl_pct", None)
            if expected_ev is not None and actual_pct is not None:
                gaps.append(abs(float(expected_ev) - float(actual_pct) / 100.0))
        return sum(gaps) / len(gaps) if gaps else 0.0

    @staticmethod
    def _check_learning_status() -> tuple[int, str]:
        """
        Query the TradeOutcomeTracker to see how many models have
        sufficient data (≥5 outcomes) for adaptive weighting.
        Returns (count_of_active_models, summary_string).
        """
        try:
            from core.meta_decision.confluence_scorer import get_outcome_tracker
            tracker  = get_outcome_tracker()
            all_models = [
                "trend", "mean_reversion", "momentum_breakout",
                "vwap_reversion", "liquidity_sweep",
                "funding_rate", "order_book", "sentiment",
            ]
            active   = []
            warming  = []
            for m in all_models:
                wr = tracker.get_win_rate(m)
                if wr is not None:
                    adj = tracker.get_weight_adjustment(m)
                    active.append(f"{m}={wr*100:.0f}% (adj×{adj:.2f})")
                else:
                    outcomes = tracker._outcomes.get(m, [])
                    if outcomes:
                        warming.append(f"{m}({len(outcomes)}/5)")
            parts = []
            if active:
                parts.append(f"Active: {', '.join(active)}")
            if warming:
                parts.append(f"Warming up: {', '.join(warming)}")
            if not active and not warming:
                parts.append("No trade outcomes recorded yet — loop will activate after first trades")
            return len(active), "  |  ".join(parts) if parts else "—"
        except Exception as exc:
            return 0, f"Learning status unavailable: {exc}"

    @staticmethod
    def _compute_condition_diversity(trades: list[dict]) -> dict:
        """
        Count distinct (model, regime) pairs that have ≥5 trades.
        Returns {qualified_count, qualified_pairs, details}.
        """
        pair_counts: dict[str, int] = {}
        for tr in trades:
            regime = (tr.get("regime") or "unknown").lower()
            for model in (tr.get("models_fired") or []):
                key = f"{model}×{regime}"
                pair_counts[key] = pair_counts.get(key, 0) + 1
        qualified = {k: v for k, v in pair_counts.items() if v >= 5}
        return {
            "qualified_count": len(qualified),
            "qualified_pairs": sorted(qualified.keys()),
            "details": {k: v for k, v in sorted(pair_counts.items())},
        }

    @staticmethod
    def _compute_regime_win_rates(trades: list[dict]) -> dict:
        """
        Compute per-regime win/loss rates.
        Flag any regime with ≥10 trades and >70% loss rate.
        """
        regime_data: dict[str, dict] = {}
        for tr in trades:
            regime = (tr.get("regime") or "unknown").lower()
            if regime not in regime_data:
                regime_data[regime] = {"wins": 0, "losses": 0}
            if (tr.get("pnl_pct") or 0) > 0:
                regime_data[regime]["wins"] += 1
            else:
                regime_data[regime]["losses"] += 1

        breakdown = {}
        has_failing = False
        worst_regime = "—"
        worst_lr     = 0.0

        for regime, data in sorted(regime_data.items()):
            total = data["wins"] + data["losses"]
            wr = data["wins"] / total if total > 0 else 0.0
            lr = 1.0 - wr
            breakdown[regime] = {
                "wins": data["wins"], "losses": data["losses"],
                "total": total, "win_rate": round(wr * 100, 1),
                "loss_rate": round(lr * 100, 1),
            }
            if total >= 10 and lr > 0.70:
                has_failing = True
            if total >= 10 and lr > worst_lr:
                worst_lr = lr
                worst_regime = regime

        return {
            "breakdown": breakdown,
            "has_failing_regime": has_failing,
            "worst_regime": worst_regime,
            "worst_loss_rate": round(worst_lr * 100, 1),
        }

    @staticmethod
    def _compute_streaks(trades: list[dict]) -> dict:
        """
        Compute max consecutive wins and losses (chronologically ordered).
        """
        if not trades:
            return {"max_win_streak": 0, "max_loss_streak": 0}

        sorted_t = sorted(trades, key=lambda t: t.get("closed_at", ""))
        max_win = max_loss = 0
        cur_win = cur_loss = 0
        for tr in sorted_t:
            if (tr.get("pnl_pct") or 0) > 0:
                cur_win += 1
                cur_loss = 0
            else:
                cur_loss += 1
                cur_win = 0
            max_win  = max(max_win, cur_win)
            max_loss = max(max_loss, cur_loss)

        return {"max_win_streak": max_win, "max_loss_streak": max_loss}

    @staticmethod
    def _check_rl_shadow() -> dict:
        """
        Query TradeMonitor for RL shadow stats and compute whether
        RL's recommendations would have improved outcomes.
        """
        try:
            from core.monitoring.trade_monitor import get_trade_monitor
            stats = get_trade_monitor().get_rl_shadow_stats()
            total = stats.get("total", 0)
            if total == 0:
                return {"total": 0, "aligned_wr": 0, "misaligned_wr": 0, "rl_boost": 0}

            aligned_total = stats.get("rl_aligned_total", 0)
            aligned_wr    = stats.get("rl_aligned_win_rate", 0) * 100

            mis_wins   = stats.get("rl_misaligned_wins", 0)
            mis_losses = stats.get("rl_misaligned_losses", 0)
            mis_total  = mis_wins + mis_losses
            misaligned_wr = (mis_wins / mis_total * 100) if mis_total > 0 else 0.0

            # RL boost = aligned_wr - misaligned_wr (positive = RL helps)
            rl_boost = aligned_wr - misaligned_wr

            return {
                "total":         total,
                "aligned_wr":    round(aligned_wr, 1),
                "misaligned_wr": round(misaligned_wr, 1),
                "rl_boost":      round(rl_boost, 1),
                "aligned_total": aligned_total,
                "misaligned_total": mis_total,
                "recommendation": (
                    "Consider re-enabling RL at weight 0.10–0.15"
                    if rl_boost >= 5 and aligned_total >= 15
                    else "Keep RL disabled — insufficient evidence of improvement"
                ),
            }
        except Exception as exc:
            return {"total": 0, "aligned_wr": 0, "misaligned_wr": 0, "rl_boost": 0,
                    "error": str(exc)}

    @staticmethod
    def _check_l2_status() -> tuple[int, int, str]:
        """
        Query the Level2PerformanceTracker to see how many contextual
        cells are active (regime × model and asset × model).
        Returns (active_cells, total_cells, summary_string).
        """
        try:
            from core.learning.level2_tracker import get_level2_tracker
            l2      = get_level2_tracker()
            summary = l2.get_summary()
            active  = (summary.get("regime_cells_active", 0)
                       + summary.get("asset_cells_active", 0))
            warming = (summary.get("regime_cells_warming", 0)
                       + summary.get("asset_cells_warming", 0))
            total   = summary.get("total_cells", 0)
            score_active = summary.get("score_bins_active", 0)
            parts = []
            if active:
                parts.append(f"{active} active cells")
            if warming:
                parts.append(f"{warming} warming")
            if score_active:
                parts.append(f"{score_active} score-calibration bin(s) active")
            if not parts:
                parts.append("No L2 data yet — will activate after ≥10 trades per cell")
            return active, total, "  |  ".join(parts)
        except Exception as exc:
            return 0, 0, f"L2 status unavailable: {exc}"


# ── Module-level singleton ───────────────────────────────────────────────────
_evaluator = DemoPerformanceEvaluator()


def get_evaluator() -> DemoPerformanceEvaluator:
    """Return the module-level evaluator singleton."""
    return _evaluator
