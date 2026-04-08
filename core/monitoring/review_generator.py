# ============================================================
# NEXUS TRADER — Review Generator
#
# Produces structured daily and weekly performance reviews
# drawing from PaperExecutor closed trades, LiveVsBacktestTracker,
# PerformanceThresholdEvaluator, and ScaleManager.
#
# Output format: plain text suitable for logging or display.
# No Qt dependency — importable in headless environments.
#
# DOES NOT MODIFY STRATEGY OR PARAMETERS.
# ============================================================
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_REPORTS_DIR = Path("reports/reviews")
_LINE = "─" * 60


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _fmt_pct(val: Optional[float], decimals: int = 1) -> str:
    if val is None:
        return "—"
    return f"{float(val) * 100:.{decimals}f}%"


def _fmt_r(val: Optional[float]) -> str:
    if val is None:
        return "—"
    sign = "+" if val > 0 else ""
    return f"{sign}{val:.3f}R"


def _fmt_usdt(val: Optional[float]) -> str:
    if val is None:
        return "—"
    sign = "+" if val >= 0 else ""
    return f"{sign}${val:,.2f}"


def _fmt_pf(val: Optional[float]) -> str:
    if val is None:
        return "—"
    return f"{val:.3f}"


def _rag_bullet(status: str) -> str:
    return {"GREEN": "🟢", "AMBER": "🟡", "RED": "🔴"}.get(status, "⚪")


# ── Anomaly scanner ───────────────────────────────────────────────────────────

def _find_anomalies(trades: list[dict], window: int = 50) -> list[str]:
    """
    Scan the last `window` trades for notable anomalies.
    Returns a list of human-readable anomaly strings.
    """
    if not trades:
        return []

    recent = trades[-window:]
    anomalies: list[str] = []

    # Consecutive losses
    streak = 0
    max_streak = 0
    for t in recent:
        pnl = t.get("pnl_usdt") or 0
        if pnl < 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    if max_streak >= 5:
        anomalies.append(f"Max consecutive loss streak: {max_streak}")

    # Rolling drawdown (cumulative R from peak)
    r_vals = [float(t.get("realized_r") or 0.0) for t in recent]
    if r_vals:
        cum   = 0.0
        peak  = 0.0
        worst = 0.0
        for r in r_vals:
            cum  += r
            peak  = max(peak, cum)
            worst = min(worst, cum - peak)
        if worst <= -3.0:
            anomalies.append(f"Rolling window max drawdown: {worst:.2f}R")

    # Slippage outliers
    slippage_vals = [float(t.get("slippage_pct") or 0.0) for t in recent if t.get("slippage_pct") is not None]
    if slippage_vals:
        avg_slip = sum(slippage_vals) / len(slippage_vals)
        if avg_slip > 0.003:   # > 0.3%
            anomalies.append(f"High average slippage: {avg_slip*100:.2f}%")

    # Expected vs realized R shortfall
    pairs = [(float(t.get("expected_rr") or 0), float(t.get("realized_r") or 0))
             for t in recent
             if t.get("expected_rr") and t.get("realized_r") is not None]
    if len(pairs) >= 10:
        avg_exp  = sum(p[0] for p in pairs) / len(pairs)
        avg_real = sum(p[1] for p in pairs) / len(pairs)
        if avg_exp > 0 and avg_real < avg_exp * 0.5:
            anomalies.append(
                f"Target capture shortfall: avg realised {avg_real:.2f}R vs "
                f"expected {avg_exp:.2f}R ({avg_real/avg_exp*100:.0f}% capture)"
            )

    return anomalies


# ── Per-symbol breakdown ──────────────────────────────────────────────────────

def _symbol_breakdown(trades: list[dict]) -> dict[str, dict]:
    """Return per-symbol stats dict: {symbol: {trades, wins, pnl_usdt, avg_r, pf}}."""
    result: dict[str, dict] = {}
    for t in trades:
        sym = str(t.get("symbol") or "unknown")
        if sym not in result:
            result[sym] = {"trades": 0, "wins": 0, "gross_win": 0.0, "gross_loss": 0.0,
                           "pnl_usdt": 0.0, "r_sum": 0.0}
        d = result[sym]
        d["trades"] += 1
        pnl  = float(t.get("pnl_usdt") or 0.0)
        r    = float(t.get("realized_r") or 0.0)
        d["pnl_usdt"] += pnl
        d["r_sum"]    += r
        if pnl > 0:
            d["wins"]      += 1
            d["gross_win"] += pnl
        else:
            d["gross_loss"] += abs(pnl)

    for sym, d in result.items():
        n = d["trades"]
        d["win_rate"] = d["wins"] / n if n else None
        d["avg_r"]    = d["r_sum"] / n if n else None
        d["pf"]       = (d["gross_win"] / d["gross_loss"]
                         if d["gross_loss"] > 0 else None)
    return result


# ── Slippage analysis ─────────────────────────────────────────────────────────

def _slippage_analysis(trades: list[dict]) -> dict:
    vals = [float(t.get("slippage_pct") or 0.0)
            for t in trades if t.get("slippage_pct") is not None]
    if not vals:
        return {"count": 0, "avg": None, "max": None, "min": None}
    return {
        "count": len(vals),
        "avg":   sum(vals) / len(vals),
        "max":   max(vals),
        "min":   min(vals),
    }


# ── Review builder ────────────────────────────────────────────────────────────

def _format_model_table(per_model: dict) -> str:
    """Return a text table of per-model metrics from LiveVsBacktestTracker comparison."""
    if not per_model:
        return "  No model-level data yet.\n"
    lines = [f"  {'Model':<22} {'Trades':>6} {'WR':>7} {'PF':>7} {'AvgR':>8} {'vs Study4':>10}"]
    lines.append("  " + "─" * 64)
    for mkey, comp in per_model.items():
        live = comp.get("live", {})
        base = comp.get("baseline", {})
        n    = int(live.get("trades") or 0)
        wr   = live.get("win_rate")
        pf   = live.get("profit_factor")
        ar   = live.get("avg_r")
        b_ar = base.get("avg_r") if base else None
        delta_ar = (ar - b_ar if ar is not None and b_ar is not None else None)
        lines.append(
            f"  {mkey:<22} {n:>6} "
            f"{_fmt_pct(wr):>7} {_fmt_pf(pf):>7} "
            f"{_fmt_r(ar):>8} {_fmt_r(delta_ar):>10}"
        )
    return "\n".join(lines) + "\n"


def _format_rag_section(assessment) -> str:
    """Return a text representation of a PortfolioRAGAssessment."""
    try:
        port   = assessment.portfolio
        lines  = []
        lines.append(f"  Portfolio: {_rag_bullet(port.overall.value)} {port.overall.value} "
                     f"({port.trades} trades)")
        lines.append(f"    WR     : {_rag_bullet(port.wr.status.value)}  "
                     f"{_fmt_pct(port.wr.value)} (GREEN ≥ {_fmt_pct(port.wr.green_min)})")
        lines.append(f"    PF     : {_rag_bullet(port.pf.status.value)}  "
                     f"{_fmt_pf(port.pf.value)} (GREEN ≥ {_fmt_pf(port.pf.green_min)})")
        lines.append(f"    Avg R  : {_rag_bullet(port.avg_r.status.value)}  "
                     f"{_fmt_r(port.avg_r.value)} (GREEN ≥ {_fmt_r(port.avg_r.green_min)})")
        if assessment.should_pause:
            lines.append(f"  ⚠️  PAUSE RECOMMENDED: {assessment.pause_reason}")
        return "\n".join(lines) + "\n"
    except Exception as exc:
        return f"  (RAG assessment unavailable: {exc})\n"


# ── Public API ────────────────────────────────────────────────────────────────

def generate_daily_review(save: bool = True) -> str:
    """
    Generate a daily performance review covering:
    - Total trades, WR, PF, capital change
    - Per-model breakdown
    - Anomaly scan
    - RAG status
    - Scale phase
    - Top anomalies

    Returns the review as a string.  Optionally saves to reports/reviews/.
    """
    from core.monitoring.performance_thresholds import get_threshold_evaluator
    from core.monitoring.live_vs_backtest       import get_live_vs_backtest_tracker
    from core.monitoring.scale_manager          import get_scale_manager

    now    = _now_utc()
    today  = now.strftime("%Y-%m-%d")
    cutoff = now - timedelta(hours=24)

    # ── Fetch trades ──────────────────────────────────────────────────────────
    try:
        from core.execution.order_router import order_router
        all_trades = list(order_router.active_executor.get_closed_trades())
    except Exception:
        all_trades = []

    # Today's trades (last 24h)
    day_trades = [
        t for t in all_trades
        if _trade_after(t, cutoff)
    ]

    # ── Metrics ───────────────────────────────────────────────────────────────
    n        = len(day_trades)
    wins     = sum(1 for t in day_trades if (t.get("pnl_usdt") or 0) > 0)
    pnl_sum  = sum(float(t.get("pnl_usdt") or 0) for t in day_trades)
    wr       = wins / n if n else None
    gross_w  = sum(float(t.get("pnl_usdt") or 0) for t in day_trades if (t.get("pnl_usdt") or 0) > 0)
    gross_l  = sum(abs(float(t.get("pnl_usdt") or 0)) for t in day_trades if (t.get("pnl_usdt") or 0) < 0)
    pf       = gross_w / gross_l if gross_l > 0 else None
    r_vals   = [float(t.get("realized_r") or 0) for t in day_trades]
    avg_r    = sum(r_vals) / len(r_vals) if r_vals else None

    # Capital
    try:
        from core.execution.order_router import order_router
        pe = order_router.active_executor
        cap_now  = float(pe._capital)
        cap_peak = float(pe._peak_capital)
        dd_pct   = (cap_peak - cap_now) / cap_peak if cap_peak > 0 else 0
    except Exception:
        cap_now = cap_peak = dd_pct = None

    # RAG
    try:
        assessment = get_threshold_evaluator().evaluate()
    except Exception:
        assessment = None

    # Scale
    try:
        scale = get_scale_manager().get_phase_summary()
    except Exception:
        scale = None

    # LvB comparison
    try:
        comp = get_live_vs_backtest_tracker().get_comparison()
        per_model_comp = comp.get("per_model", {})
    except Exception:
        per_model_comp = {}

    # Anomalies
    anomalies = _find_anomalies(day_trades)

    # ── Build report ──────────────────────────────────────────────────────────
    lines: list[str] = []
    lines.append(_LINE)
    lines.append(f"NEXUS TRADER — Daily Review  |  {today} UTC")
    lines.append(_LINE)

    lines.append("\n📊 TODAY'S PERFORMANCE")
    lines.append(f"  Trades         : {n}")
    lines.append(f"  Win Rate       : {_fmt_pct(wr)}")
    lines.append(f"  Profit Factor  : {_fmt_pf(pf)}")
    lines.append(f"  Avg R          : {_fmt_r(avg_r)}")
    lines.append(f"  P&L            : {_fmt_usdt(pnl_sum)}")
    if cap_now is not None:
        lines.append(f"  Capital        : {_fmt_usdt(cap_now)}")
        lines.append(f"  Drawdown       : {_fmt_pct(dd_pct)} from peak")

    lines.append("\n🧩 MODEL BREAKDOWN (today)")
    if day_trades:
        sym_map = _symbol_breakdown(day_trades)
        for sym, d in sorted(sym_map.items()):
            lines.append(
                f"  {sym:<16} {d['trades']:>3} trades | "
                f"WR {_fmt_pct(d['win_rate'])} | "
                f"PF {_fmt_pf(d['pf'])} | "
                f"P&L {_fmt_usdt(d['pnl_usdt'])}"
            )
    else:
        lines.append("  No trades today.")

    lines.append("\n🟢 RAG STATUS (rolling)")
    if assessment:
        lines.append(_format_rag_section(assessment))
    else:
        lines.append("  (unavailable)")

    if scale:
        lines.append(f"\n⚙️  SCALE PHASE: {scale['description']}")
        lines.append(f"  Risk per trade : {scale['risk_pct_str']}")

    if anomalies:
        lines.append("\n⚠️  ANOMALIES")
        for a in anomalies:
            lines.append(f"  • {a}")
    else:
        lines.append("\n✅ ANOMALIES: None detected")

    lines.append(_LINE)
    report = "\n".join(lines)

    if save:
        _save_report(report, f"daily_{today}.txt")

    return report


def generate_weekly_review(save: bool = True) -> str:
    """
    Generate a weekly performance review covering:
    - Full performance summary vs Study 4 baselines
    - Per-symbol breakdown
    - Per-model breakdown
    - Slippage analysis
    - Anomaly scan
    - RAG status + scale recommendation

    Returns the review as a string.  Optionally saves to reports/reviews/.
    """
    from core.monitoring.performance_thresholds import get_threshold_evaluator
    from core.monitoring.live_vs_backtest       import get_live_vs_backtest_tracker
    from core.monitoring.scale_manager          import get_scale_manager

    now    = _now_utc()
    today  = now.strftime("%Y-%m-%d")
    cutoff = now - timedelta(days=7)

    # ── Fetch trades ──────────────────────────────────────────────────────────
    try:
        from core.execution.order_router import order_router
        all_trades = list(order_router.active_executor.get_closed_trades())
    except Exception:
        all_trades = []

    week_trades = [t for t in all_trades if _trade_after(t, cutoff)]
    n           = len(week_trades)

    # Overall metrics
    wins    = sum(1 for t in week_trades if (t.get("pnl_usdt") or 0) > 0)
    pnl_sum = sum(float(t.get("pnl_usdt") or 0) for t in week_trades)
    wr      = wins / n if n else None
    gross_w = sum(float(t.get("pnl_usdt") or 0) for t in week_trades if (t.get("pnl_usdt") or 0) > 0)
    gross_l = sum(abs(float(t.get("pnl_usdt") or 0)) for t in week_trades if (t.get("pnl_usdt") or 0) < 0)
    pf      = gross_w / gross_l if gross_l > 0 else None
    r_vals  = [float(t.get("realized_r") or 0) for t in week_trades]
    avg_r   = sum(r_vals) / len(r_vals) if r_vals else None

    # Capital
    try:
        from core.execution.order_router import order_router
        pe        = order_router.active_executor
        cap_now   = float(pe._capital)
        cap_start = 100_000.0   # baseline starting capital
        cap_peak  = float(pe._peak_capital)
        dd_pct    = (cap_peak - cap_now) / cap_peak if cap_peak > 0 else 0
        total_ret = (cap_now - cap_start) / cap_start if cap_start > 0 else 0
    except Exception:
        cap_now = cap_peak = dd_pct = total_ret = None

    # LvB comparison
    try:
        comp           = get_live_vs_backtest_tracker().get_comparison()
        port_live      = comp.get("portfolio", {}).get("live", {})
        port_base      = comp.get("portfolio", {}).get("baseline", {})
        port_delta     = comp.get("portfolio", {}).get("delta", {})
        per_model_comp = comp.get("per_model", {})
    except Exception:
        port_live = port_base = port_delta = {}
        per_model_comp = {}

    # RAG
    try:
        assessment = get_threshold_evaluator().evaluate()
    except Exception:
        assessment = None

    # Scale
    try:
        sm        = get_scale_manager()
        scale     = sm.get_phase_summary()
        scale_eval = sm.evaluate_advancement()
    except Exception:
        scale = None
        scale_eval = None

    # Anomalies over week
    anomalies = _find_anomalies(week_trades, window=200)

    # Slippage
    slip = _slippage_analysis(week_trades)

    # Symbol breakdown
    sym_data = _symbol_breakdown(week_trades) if week_trades else {}

    # ── Build report ──────────────────────────────────────────────────────────
    lines: list[str] = []
    lines.append(_LINE)
    lines.append(f"NEXUS TRADER — Weekly Review  |  Week ending {today} UTC")
    lines.append(_LINE)

    lines.append("\n📊 WEEKLY PERFORMANCE")
    lines.append(f"  Trades         : {n}")
    lines.append(f"  Win Rate       : {_fmt_pct(wr)}")
    lines.append(f"  Profit Factor  : {_fmt_pf(pf)}")
    lines.append(f"  Avg R          : {_fmt_r(avg_r)}")
    lines.append(f"  P&L            : {_fmt_usdt(pnl_sum)}")
    if cap_now is not None:
        lines.append(f"  Capital        : {_fmt_usdt(cap_now)}")
        lines.append(f"  Total Return   : {_fmt_pct(total_ret)} from $100,000")
        lines.append(f"  Drawdown       : {_fmt_pct(dd_pct)} from peak")

    lines.append("\n📈 VS STUDY 4 BASELINES")
    lines.append(f"  {'Metric':<12} {'Live':>9} {'Baseline':>10} {'Delta':>10}")
    lines.append("  " + "─" * 44)
    _base_wr   = port_base.get("win_rate")
    _base_pf   = port_base.get("profit_factor")
    _base_ar   = port_base.get("avg_r")
    _live_wr   = port_live.get("win_rate")
    _live_pf   = port_live.get("profit_factor")
    _live_ar   = port_live.get("avg_r")
    lines.append(
        f"  {'WR':<12} {_fmt_pct(_live_wr):>9} {_fmt_pct(_base_wr):>10} "
        f"{_fmt_pct(port_delta.get('wr_delta')):>10}"
    )
    lines.append(
        f"  {'PF':<12} {_fmt_pf(_live_pf):>9} {_fmt_pf(_base_pf):>10} "
        f"{_fmt_pf(port_delta.get('pf_delta')):>10}"
    )
    lines.append(
        f"  {'Avg R':<12} {_fmt_r(_live_ar):>9} {_fmt_r(_base_ar):>10} "
        f"{_fmt_r(port_delta.get('avg_r_delta')):>10}"
    )

    lines.append("\n🧩 PER-MODEL BREAKDOWN (rolling)")
    lines.append(_format_model_table(per_model_comp))

    lines.append("📍 PER-SYMBOL BREAKDOWN (weekly)")
    if sym_data:
        lines.append(f"  {'Symbol':<16} {'Trades':>6} {'WR':>7} {'PF':>7} {'AvgR':>8} {'P&L':>12}")
        lines.append("  " + "─" * 62)
        for sym, d in sorted(sym_data.items(), key=lambda x: x[1]["pnl_usdt"], reverse=True):
            lines.append(
                f"  {sym:<16} {d['trades']:>6} "
                f"{_fmt_pct(d['win_rate']):>7} {_fmt_pf(d['pf']):>7} "
                f"{_fmt_r(d['avg_r']):>8} {_fmt_usdt(d['pnl_usdt']):>12}"
            )
    else:
        lines.append("  No trades this week.")

    lines.append("\n📦 SLIPPAGE ANALYSIS (weekly)")
    if slip["count"] > 0:
        lines.append(f"  Avg: {slip['avg']*100:.3f}%  |  "
                     f"Max: {slip['max']*100:.3f}%  |  "
                     f"Min: {slip['min']*100:.3f}%  |  "
                     f"N: {slip['count']}")
    else:
        lines.append("  No slippage data.")

    lines.append("\n🟢 RAG STATUS")
    if assessment:
        lines.append(_format_rag_section(assessment))
    else:
        lines.append("  (unavailable)")

    if scale:
        lines.append(f"\n⚙️  SCALE PHASE: {scale['description']}")
        lines.append(f"  Risk per trade : {scale['risk_pct_str']}")
    if scale_eval:
        lines.append(f"  Advancement    : {scale_eval.recommendation}")

    if anomalies:
        lines.append("\n⚠️  ANOMALIES")
        for a in anomalies:
            lines.append(f"  • {a}")
    else:
        lines.append("\n✅ ANOMALIES: None detected")

    lines.append(_LINE)
    report = "\n".join(lines)

    if save:
        _save_report(report, f"weekly_{today}.txt")

    return report


# ── Save helper ───────────────────────────────────────────────────────────────

def _save_report(text: str, filename: str) -> None:
    try:
        _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        path = _REPORTS_DIR / filename
        path.write_text(text, encoding="utf-8")
        logger.info("ReviewGenerator: saved %s", path)
    except Exception as exc:
        logger.debug("ReviewGenerator: save failed (non-fatal): %s", exc)


def _trade_after(trade: dict, cutoff: datetime) -> bool:
    """Return True if the trade's closed_at timestamp is after cutoff."""
    raw = trade.get("closed_at") or trade.get("opened_at") or ""
    if not raw:
        return True   # include trades with no timestamp
    try:
        if isinstance(raw, datetime):
            dt = raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
        else:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return dt >= cutoff
    except Exception:
        return True   # be inclusive on parse failures
