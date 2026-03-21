"""
System Readiness Evaluator — Session 23.

Provides a single, unambiguous verdict on whether the system has accumulated
sufficient evidence to move from demo paper-trading into cautious live trading.

This complements DemoPerformanceEvaluator (which checks the 15-point readiness
checklist) with a simpler, human-readable three-level verdict that directly
maps to actionable risk decisions.

Levels:
  STILL_LEARNING  — Not enough trades, or key metrics negative.
                    Action: continue paper-trading, no live trading consideration.

  IMPROVING       — Enough trades (≥ 75), all key metrics positive or neutral.
                    Action: observe one more week; tighten monitoring.

  READY_FOR_CAUTIOUS_LIVE — Strong across all criteria.
                    Action: consider live trading with strict risk controls.

Criteria per level (all must be met):
  STILL_LEARNING (default until surpassed):
    - trades < 75, OR
    - expectancy E[R] ≤ 0, OR
    - max drawdown_r ≥ 10R (catastrophic risk)

  IMPROVING (must pass STILL_LEARNING gates AND):
    - trades ≥ 75
    - E[R] > 0.0R
    - profit_factor > 1.10
    - max drawdown_r < 10R
    - calibrator AUC ≥ 0.50 (if calibrator is trained)

  READY_FOR_CAUTIOUS_LIVE (must pass IMPROVING gates AND):
    - trades ≥ 100
    - E[R] ≥ 0.20R
    - profit_factor ≥ 1.40
    - max drawdown_r < 7R
    - win_rate ≥ 0.45
    - calibrator AUC ≥ 0.55 (if calibrator has ≥ 50 predictions)
    - No single regime comprising > 80% of winning trades

The evaluator reads directly from the PaperExecutor trade history —
no separate data pipeline needed.  It is read-only and has no side effects.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class SystemReadinessLevel(str, Enum):
    STILL_LEARNING          = "STILL_LEARNING"
    IMPROVING               = "IMPROVING"
    READY_FOR_CAUTIOUS_LIVE = "READY_FOR_CAUTIOUS_LIVE"


@dataclass
class ReadinessCheck:
    name:    str
    passed:  bool
    value:   object    # actual measured value
    target:  object    # required threshold
    note:    str = ""


@dataclass
class SystemReadinessAssessment:
    level:          SystemReadinessLevel
    score:          float            # 0–100 composite score
    checks:         list[ReadinessCheck] = field(default_factory=list)
    summary:        str = ""
    action:         str = ""         # human-readable recommended action


class SystemReadinessEvaluator:
    """
    Evaluates system readiness from trade history.

    Call evaluate() with a list of closed-trade dicts (same format as
    PaperExecutor.get_history()).
    """

    def evaluate(self, trades: list[dict]) -> SystemReadinessAssessment:
        """
        Evaluate system readiness from a list of closed-trade dicts.

        Parameters
        ----------
        trades : list[dict]
            Closed trade records from PaperExecutor.get_history().
            Required keys: pnl_pct, pnl_usdt, entry_price, stop_loss,
                           take_profit, side, regime, models_fired, score.
        """
        try:
            return self._evaluate_impl(trades)
        except Exception as exc:
            logger.warning("SystemReadinessEvaluator: error: %s", exc)
            return SystemReadinessAssessment(
                level=SystemReadinessLevel.STILL_LEARNING,
                score=0.0,
                summary=f"Evaluation error: {exc}",
                action="Check logs for details.",
            )

    def _evaluate_impl(self, trades: list[dict]) -> SystemReadinessAssessment:
        checks: list[ReadinessCheck] = []

        n       = len(trades)
        wins    = [t for t in trades if (t.get("pnl_pct") or 0) > 0]
        win_rate = len(wins) / n if n > 0 else 0.0

        # ── Expectancy ──────────────────────────────────────────────────────
        r_list = self._compute_r_multiples(trades)
        exp_r  = round(sum(r_list) / len(r_list), 4) if r_list else None

        # ── Profit Factor ───────────────────────────────────────────────────
        gross_win  = sum(r for r in r_list if r > 0)
        gross_loss = sum(abs(r) for r in r_list if r < 0)
        pf = round(gross_win / gross_loss, 4) if gross_loss > 0 else (None if not r_list else 999.0)

        # ── Max Drawdown in R ───────────────────────────────────────────────
        max_dd_r = self._max_drawdown_r(r_list)

        # ── Regime concentration ────────────────────────────────────────────
        regime_win_counts: dict[str, int] = {}
        for t in wins:
            reg = t.get("regime", "unknown") or "unknown"
            regime_win_counts[reg] = regime_win_counts.get(reg, 0) + 1
        regime_max_pct = (
            max(regime_win_counts.values()) / len(wins)
            if wins else 0.0
        )

        # ── Calibrator AUC ─────────────────────────────────────────────────
        cal_auc: Optional[float] = None
        cal_count = 0
        try:
            from core.learning.calibrator_monitor import get_calibrator_monitor
            status = get_calibrator_monitor().get_status()
            cal_auc   = status.get("auc")
            cal_count = status.get("prediction_count", 0)
        except Exception:
            pass

        # ── Build checks (STILL_LEARNING gates) ────────────────────────────
        checks.append(ReadinessCheck(
            name="Minimum trades",
            passed=n >= 75,
            value=n,
            target="≥ 75",
            note="Need sufficient trade count for statistical significance.",
        ))
        checks.append(ReadinessCheck(
            name="Positive expectancy E[R]",
            passed=exp_r is not None and exp_r > 0.0,
            value=f"{exp_r:.3f}R" if exp_r is not None else "N/A",
            target="> 0.0R",
            note="System must have positive average R per trade.",
        ))
        checks.append(ReadinessCheck(
            name="Drawdown R < 10R",
            passed=max_dd_r is not None and max_dd_r < 10.0,
            value=f"{max_dd_r:.2f}R" if max_dd_r is not None else "N/A",
            target="< 10R",
            note="Catastrophic drawdown gate — blocks IMPROVING regardless.",
        ))

        # ── STILL_LEARNING verdict ──────────────────────────────────────────
        sl_gates = [c for c in checks]
        still_learning = any(not c.passed for c in sl_gates)

        if still_learning:
            failed_checks = [c.name for c in sl_gates if not c.passed]
            score = round(sum(1 for c in checks if c.passed) / len(checks) * 40, 1)
            return SystemReadinessAssessment(
                level=SystemReadinessLevel.STILL_LEARNING,
                score=score,
                checks=checks,
                summary=f"STILL LEARNING — failed gates: {', '.join(failed_checks)}",
                action=(
                    "Continue paper-trading.  "
                    "Focus on accumulating ≥ 75 trades across diverse market conditions."
                ),
            )

        # ── IMPROVING checks ────────────────────────────────────────────────
        checks.append(ReadinessCheck(
            name="Profit Factor > 1.10",
            passed=pf is not None and pf > 1.10,
            value=f"{pf:.2f}" if pf is not None else "N/A",
            target="> 1.10",
        ))
        checks.append(ReadinessCheck(
            name="Calibrator AUC ≥ 0.50",
            passed=cal_auc is None or cal_count < 20 or cal_auc >= 0.50,
            value=f"{cal_auc:.3f}" if cal_auc is not None else "untrained",
            target="≥ 0.50 (if trained)",
            note="Waived if calibrator has fewer than 20 predictions.",
        ))

        improving_checks = [c for c in checks[3:]]  # newly added
        improving_failed = [c for c in improving_checks if not c.passed]

        if improving_failed:
            score = round(sum(1 for c in checks if c.passed) / len(checks) * 65, 1)
            return SystemReadinessAssessment(
                level=SystemReadinessLevel.IMPROVING,
                score=score,
                checks=checks,
                summary=(
                    f"IMPROVING — passed initial gates but needs: "
                    f"{', '.join(c.name for c in improving_failed)}"
                ),
                action=(
                    "On the right track.  Monitor for one more week before "
                    "considering live capital."
                ),
            )

        # Also confirm IMPROVING if all basic improving checks pass
        improving_score = round(sum(1 for c in checks if c.passed) / len(checks) * 65, 1)
        if improving_score < 65:
            return SystemReadinessAssessment(
                level=SystemReadinessLevel.IMPROVING,
                score=improving_score,
                checks=checks,
                summary="IMPROVING — early-stage metrics positive but not yet strong enough.",
                action="Monitor for 1–2 more weeks.",
            )

        # ── READY checks ────────────────────────────────────────────────────
        checks.append(ReadinessCheck(
            name="Trade count ≥ 100",
            passed=n >= 100,
            value=n,
            target="≥ 100",
        ))
        checks.append(ReadinessCheck(
            name="Expectancy ≥ 0.20R",
            passed=exp_r is not None and exp_r >= 0.20,
            value=f"{exp_r:.3f}R" if exp_r is not None else "N/A",
            target="≥ 0.20R",
        ))
        checks.append(ReadinessCheck(
            name="Profit Factor ≥ 1.40",
            passed=pf is not None and pf >= 1.40,
            value=f"{pf:.2f}" if pf is not None else "N/A",
            target="≥ 1.40",
        ))
        checks.append(ReadinessCheck(
            name="Win rate ≥ 45%",
            passed=win_rate >= 0.45,
            value=f"{win_rate:.1%}",
            target="≥ 45%",
        ))
        checks.append(ReadinessCheck(
            name="Drawdown R < 7R",
            passed=max_dd_r is not None and max_dd_r < 7.0,
            value=f"{max_dd_r:.2f}R" if max_dd_r is not None else "N/A",
            target="< 7R",
        ))
        checks.append(ReadinessCheck(
            name="Regime concentration ≤ 80%",
            passed=regime_max_pct <= 0.80,
            value=f"{regime_max_pct:.0%}",
            target="≤ 80%",
            note="Prevents regime-specific overfit masking poor generalisation.",
        ))
        if cal_count >= 50:
            checks.append(ReadinessCheck(
                name="Calibrator AUC ≥ 0.55 (mature)",
                passed=cal_auc is not None and cal_auc >= 0.55,
                value=f"{cal_auc:.3f}" if cal_auc is not None else "N/A",
                target="≥ 0.55",
            ))

        ready_failed = [c for c in checks[5:] if not c.passed]
        total_score  = round(sum(1 for c in checks if c.passed) / len(checks) * 100, 1)

        if ready_failed:
            return SystemReadinessAssessment(
                level=SystemReadinessLevel.IMPROVING,
                score=total_score,
                checks=checks,
                summary=(
                    f"IMPROVING (near-ready) — strong core metrics but needs: "
                    f"{', '.join(c.name for c in ready_failed)}"
                ),
                action=(
                    "Very close.  Address failing checks and re-evaluate after "
                    "10–15 more trades."
                ),
            )

        return SystemReadinessAssessment(
            level=SystemReadinessLevel.READY_FOR_CAUTIOUS_LIVE,
            score=total_score,
            checks=checks,
            summary=(
                f"READY FOR CAUTIOUS LIVE — E[R]={exp_r:.2f}R, PF={pf:.2f}, "
                f"DD={max_dd_r:.1f}R, WR={win_rate:.0%}, {n} trades"
            ),
            action=(
                "System has demonstrated consistent positive edge.  "
                "Consider live trading with strict risk controls: "
                "≤ 1% per trade, draw-down halt at 5%, weekly performance review."
            ),
        )

    # ── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _compute_r_multiples(trades: list[dict]) -> list[float]:
        """Compute R-multiple for each trade that has enough price data."""
        r_list = []
        for t in trades:
            entry = float(t.get("entry_price") or 0)
            sl    = float(t.get("stop_loss")   or 0)
            pnl_u = float(t.get("pnl_usdt")    or 0)
            size  = float(t.get("size_usdt")   or 0)
            if entry > 0 and sl > 0 and size > 0:
                risk_usdt = abs(entry - sl) / entry * size
                if risk_usdt > 0:
                    r_list.append(round(pnl_u / risk_usdt, 4))
        return r_list

    @staticmethod
    def _max_drawdown_r(r_list: list[float]) -> Optional[float]:
        """Max peak-to-trough drawdown in cumulative R space."""
        if not r_list:
            return None
        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0
        for r in r_list:
            cumulative += r
            if cumulative > peak:
                peak = cumulative
            dd = peak - cumulative
            if dd > max_dd:
                max_dd = dd
        return round(max_dd, 4)


# ── Module-level singleton ──────────────────────────────────────────────────

_evaluator_instance: Optional[SystemReadinessEvaluator] = None


def get_system_readiness_evaluator() -> SystemReadinessEvaluator:
    """Return the module-level SystemReadinessEvaluator singleton."""
    global _evaluator_instance
    if _evaluator_instance is None:
        _evaluator_instance = SystemReadinessEvaluator()
    return _evaluator_instance
