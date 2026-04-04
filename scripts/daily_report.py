#!/usr/bin/env python3
"""
NexusTrader Daily Paper Trading Report

Generates comprehensive operational reports from paper trading database.

CLI:
    python scripts/daily_report.py                  # today
    python scripts/daily_report.py --date 2026-03-25  # specific date
    python scripts/daily_report.py --all-time        # all trades ever
    python scripts/daily_report.py --html            # also save HTML
    python scripts/daily_report.py --last-n 7       # last N days
"""

import sys
import json
import argparse
from pathlib import Path
from datetime import datetime, timedelta
from collections import Counter
from dataclasses import dataclass, field

# Setup path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class TradeMetrics:
    """Container for trade performance metrics."""
    trades_opened: int = 0
    trades_closed: int = 0
    wins: int = 0
    losses: int = 0
    win_rate_pct: float = 0.0
    profit_factor: float = 0.0
    avg_pnl_usdt: float = 0.0
    total_pnl_usdt: float = 0.0
    avg_r: float = 0.0
    expectancy_r: float = 0.0
    max_drawdown_r: float = 0.0
    avg_duration_h: float = 0.0
    by_symbol: dict = field(default_factory=dict)  # symbol -> {trades, wr, pf, pnl}


@dataclass
class AnalysisMetrics:
    """Container for trade analysis quality metrics."""
    analysis_available: int = 0
    analysis_coverage_pct: float = 0.0
    good_count: int = 0
    bad_count: int = 0
    neutral_count: int = 0
    good_pct: float = 0.0
    bad_pct: float = 0.0
    neutral_pct: float = 0.0
    good_win_count: int = 0
    good_loss_count: int = 0
    bad_win_count: int = 0
    bad_loss_count: int = 0
    neutral_win_count: int = 0
    neutral_loss_count: int = 0
    avg_overall_score: float = 0.0
    avoidable_loss_count: int = 0
    avoidable_loss_pct: float = 0.0
    top_root_causes: list = field(default_factory=list)  # [(category, count), ...]
    top_recommendations: list = field(default_factory=list)  # [(rec, count), ...]


@dataclass
class ProposalMetrics:
    """Container for strategy tuning proposal metrics."""
    proposals_pending: int = 0
    proposals_approved: int = 0
    proposals_rejected: int = 0
    proposals_applied: int = 0


@dataclass
class SystemMetrics:
    """Container for system health metrics."""
    analysis_ok: int = 0
    analysis_error: int = 0
    analysis_fallback: int = 0
    notif_ok: int = 0
    notif_error: int = 0
    filter_stats_ok: int = 0
    filter_stats_error: int = 0
    contract_violations: int = 0


# ============================================================================
# Database Helpers
# ============================================================================

def _get_session():
    """Lazy import and return SQLAlchemy session."""
    from core.database.engine import get_session
    return get_session()


def _get_paper_trades(session, date_start=None, date_end=None):
    """
    Fetch paper trades within date range.

    Args:
        session: SQLAlchemy session
        date_start: ISO string or None (today)
        date_end: ISO string or None (today)

    Returns:
        List of PaperTrade ORM objects
    """
    try:
        from core.database.models import PaperTrade

        query = session.query(PaperTrade)

        if date_start and date_end:
            query = query.filter(
                (PaperTrade.closed_at >= date_start) &
                (PaperTrade.closed_at <= date_end)
            )
        elif date_start:
            query = query.filter(PaperTrade.closed_at >= date_start)
        elif date_end:
            query = query.filter(PaperTrade.closed_at <= date_end)

        return query.all()
    except Exception as e:
        print(f"  [Error loading paper trades: {e}]", file=sys.stderr)
        return []


def _get_trade_feedback(session, date_start=None, date_end=None):
    """Fetch trade feedback within date range."""
    try:
        from core.database.models import TradeFeedback

        query = session.query(TradeFeedback)

        if date_start and date_end:
            query = query.filter(
                (TradeFeedback.created_at >= date_start) &
                (TradeFeedback.created_at <= date_end)
            )
        elif date_start:
            query = query.filter(TradeFeedback.created_at >= date_start)
        elif date_end:
            query = query.filter(TradeFeedback.created_at <= date_end)

        return query.all()
    except Exception as e:
        print(f"  [Error loading trade feedback: {e}]", file=sys.stderr)
        return []


def _get_proposals(session):
    """Fetch all strategy tuning proposals (not date-filtered)."""
    try:
        from core.database.models import StrategyTuningProposal
        return session.query(StrategyTuningProposal).all()
    except Exception as e:
        print(f"  [Error loading proposals: {e}]", file=sys.stderr)
        return []


def _get_analysis_metrics_snapshot():
    """Get system health counters from analysis_metrics module."""
    try:
        from core.analysis import analysis_metrics
        snapshot = analysis_metrics.snapshot()
        return snapshot
    except Exception as e:
        print(f"  [Error loading analysis metrics: {e}]", file=sys.stderr)
        return {}


# ============================================================================
# Metrics Computation
# ============================================================================

def compute_trade_metrics(trades: list) -> TradeMetrics:
    """
    Compute trade performance metrics from list of PaperTrade objects.
    """
    metrics = TradeMetrics()

    if not trades:
        return metrics

    # Filter to closed trades only
    closed_trades = [t for t in trades if t.closed_at]

    metrics.trades_opened = len(trades)
    metrics.trades_closed = len(closed_trades)

    if not closed_trades:
        return metrics

    # Win/loss counts
    wins = [t for t in closed_trades if t.pnl_usdt > 0]
    losses = [t for t in closed_trades if t.pnl_usdt <= 0]

    metrics.wins = len(wins)
    metrics.losses = len(losses)
    metrics.win_rate_pct = (len(wins) / len(closed_trades) * 100) if closed_trades else 0.0

    # PnL metrics
    total_win_pnl = sum(t.pnl_usdt for t in wins) if wins else 0.0
    total_loss_pnl = sum(t.pnl_usdt for t in losses) if losses else 0.0

    metrics.total_pnl_usdt = total_win_pnl + total_loss_pnl
    metrics.avg_pnl_usdt = metrics.total_pnl_usdt / len(closed_trades)

    # Profit factor (avoid divide by zero)
    if total_loss_pnl < 0:
        metrics.profit_factor = abs(total_win_pnl / total_loss_pnl)
    else:
        metrics.profit_factor = 0.0 if total_win_pnl == 0 else float('inf')

    # Average R (risk units)
    r_values = []
    for t in closed_trades:
        # Use entry_size_usdt if available, else size_usdt
        entry_size = t.entry_size_usdt if t.entry_size_usdt else t.size_usdt
        if entry_size and entry_size > 0:
            # Risk is typically 0.5% of position size
            risk_per_unit = entry_size * 0.005
            r = t.pnl_usdt / risk_per_unit if risk_per_unit > 0 else 0.0
            r_values.append(r)

    metrics.avg_r = sum(r_values) / len(r_values) if r_values else 0.0

    # Expectancy (in R)
    if r_values and closed_trades:
        avg_win_r = sum(r for r in r_values if r > 0) / len([r for r in r_values if r > 0]) if [r for r in r_values if r > 0] else 0.0
        avg_loss_r = abs(sum(r for r in r_values if r <= 0) / len([r for r in r_values if r <= 0])) if [r for r in r_values if r <= 0] else 0.0
        wr = metrics.win_rate_pct / 100.0
        metrics.expectancy_r = (wr * avg_win_r) - ((1 - wr) * avg_loss_r)

    # Max drawdown (cumulative R from peak)
    cumulative_r = 0.0
    peak_r = 0.0
    max_dd = 0.0
    for r in r_values:
        cumulative_r += r
        if cumulative_r > peak_r:
            peak_r = cumulative_r
        dd = peak_r - cumulative_r
        if dd > max_dd:
            max_dd = dd
    metrics.max_drawdown_r = max_dd

    # Average duration
    durations_s = [t.duration_s for t in closed_trades if t.duration_s]
    metrics.avg_duration_h = (sum(durations_s) / len(durations_s) / 3600) if durations_s else 0.0

    # By-symbol breakdown
    by_symbol = {}
    for symbol in set(t.symbol for t in closed_trades):
        sym_trades = [t for t in closed_trades if t.symbol == symbol]
        sym_wins = len([t for t in sym_trades if t.pnl_usdt > 0])
        sym_wr = (sym_wins / len(sym_trades) * 100) if sym_trades else 0.0

        sym_win_pnl = sum(t.pnl_usdt for t in sym_trades if t.pnl_usdt > 0) or 0.0
        sym_loss_pnl = sum(t.pnl_usdt for t in sym_trades if t.pnl_usdt <= 0) or 0.0
        sym_pf = abs(sym_win_pnl / sym_loss_pnl) if sym_loss_pnl < 0 else (0.0 if sym_win_pnl == 0 else float('inf'))

        sym_pnl = sum(t.pnl_usdt for t in sym_trades)

        by_symbol[symbol] = {
            'trades': len(sym_trades),
            'wr': sym_wr,
            'pf': sym_pf,
            'pnl': sym_pnl,
        }

    metrics.by_symbol = by_symbol

    return metrics


def compute_analysis_metrics(trades: list, feedbacks: list) -> AnalysisMetrics:
    """
    Compute trade analysis quality metrics.
    """
    metrics = AnalysisMetrics()

    if not trades:
        return metrics

    closed_trades = [t for t in trades if t.closed_at]
    if not closed_trades:
        return metrics

    metrics.analysis_available = len(feedbacks)
    metrics.analysis_coverage_pct = (len(feedbacks) / len(closed_trades) * 100) if closed_trades else 0.0

    if not feedbacks:
        return metrics

    # Classification counts
    classifications = Counter(f.classification for f in feedbacks)
    metrics.good_count = classifications.get('GOOD', 0)
    metrics.bad_count = classifications.get('BAD', 0)
    metrics.neutral_count = classifications.get('NEUTRAL', 0)

    total_classified = metrics.good_count + metrics.bad_count + metrics.neutral_count
    if total_classified > 0:
        metrics.good_pct = metrics.good_count / total_classified * 100
        metrics.bad_pct = metrics.bad_count / total_classified * 100
        metrics.neutral_pct = metrics.neutral_count / total_classified * 100

    # Decision outcome matrix
    for f in feedbacks:
        matrix = f.decision_outcome_matrix or ""
        if "GOOD_WIN" in matrix:
            metrics.good_win_count += 1
        elif "GOOD_LOSS" in matrix:
            metrics.good_loss_count += 1
        elif "BAD_WIN" in matrix:
            metrics.bad_win_count += 1
        elif "BAD_LOSS" in matrix:
            metrics.bad_loss_count += 1
        elif "NEUTRAL_WIN" in matrix:
            metrics.neutral_win_count += 1
        elif "NEUTRAL_LOSS" in matrix:
            metrics.neutral_loss_count += 1

    # Average overall score
    scores = [f.overall_score for f in feedbacks if f.overall_score is not None]
    metrics.avg_overall_score = sum(scores) / len(scores) if scores else 0.0

    # Avoidable loss count
    metrics.avoidable_loss_count = sum(1 for f in feedbacks if f.avoidable_loss_flag)
    metrics.avoidable_loss_pct = (metrics.avoidable_loss_count / len(feedbacks) * 100) if feedbacks else 0.0

    # Top root causes
    all_causes = []
    for f in feedbacks:
        if f.root_causes:
            try:
                causes = json.loads(f.root_causes) if isinstance(f.root_causes, str) else f.root_causes
                all_causes.extend(causes)
            except (json.JSONDecodeError, TypeError):
                pass

    cause_counter = Counter(all_causes)
    metrics.top_root_causes = cause_counter.most_common(5)

    # Top recommendations
    all_recs = []
    for f in feedbacks:
        if f.recommendations:
            try:
                recs = json.loads(f.recommendations) if isinstance(f.recommendations, str) else f.recommendations
                all_recs.extend(recs)
            except (json.JSONDecodeError, TypeError):
                pass

    rec_counter = Counter(all_recs)
    metrics.top_recommendations = rec_counter.most_common(5)

    return metrics


def compute_proposal_metrics(proposals: list) -> ProposalMetrics:
    """
    Compute strategy tuning proposal metrics.
    """
    metrics = ProposalMetrics()

    if not proposals:
        return metrics

    status_counter = Counter(p.status for p in proposals)
    metrics.proposals_pending = status_counter.get('pending', 0)
    metrics.proposals_approved = status_counter.get('approved', 0)
    metrics.proposals_rejected = status_counter.get('rejected', 0)
    metrics.proposals_applied = status_counter.get('applied', 0)

    return metrics


def compute_system_metrics(snapshot: dict) -> SystemMetrics:
    """
    Extract system health metrics from analysis_metrics snapshot.
    """
    metrics = SystemMetrics()

    if not snapshot:
        return metrics

    metrics.analysis_ok = snapshot.get('analysis_ok', 0)
    metrics.analysis_error = snapshot.get('analysis_error', 0)
    metrics.analysis_fallback = snapshot.get('analysis_fallback', 0)
    metrics.notif_ok = snapshot.get('notif_ok', 0)
    metrics.notif_error = snapshot.get('notif_error', 0)
    metrics.filter_stats_ok = snapshot.get('filter_stats_ok', 0)
    metrics.filter_stats_error = snapshot.get('filter_stats_error', 0)
    metrics.contract_violations = snapshot.get('contract_violations', 0)

    return metrics


# ============================================================================
# Milestone Evaluation
# ============================================================================

def evaluate_milestone(stats: dict):
    """
    Call milestone evaluator if available.

    Args:
        stats: dict with keys like total_trades, win_rate_pct, profit_factor, etc.

    Returns:
        dict with milestone status, or None if module unavailable
    """
    try:
        from core.monitoring.paper_trading_monitor import evaluate_milestone as eval_fn
        result = eval_fn(stats)
        return result
    except ImportError:
        return None
    except Exception as e:
        print(f"  [Error evaluating milestone: {e}]", file=sys.stderr)
        return None


# ============================================================================
# Date Range Parsing
# ============================================================================

def parse_date_range(args) -> tuple:
    """
    Parse CLI args and return (date_start, date_end) ISO strings for DB queries.

    Returns:
        (date_start_iso, date_end_iso) or (None, None) for all-time
    """
    today = datetime.utcnow().date()

    if args.all_time:
        return (None, None)

    if args.last_n:
        date_start = today - timedelta(days=args.last_n - 1)
        date_end = today + timedelta(days=1)  # Include all of today
        return (
            date_start.isoformat() + "T00:00:00Z",
            date_end.isoformat() + "T23:59:59Z"
        )

    if args.date:
        target_date = datetime.fromisoformat(args.date).date()
    else:
        target_date = today

    return (
        target_date.isoformat() + "T00:00:00Z",
        target_date.isoformat() + "T23:59:59Z"
    )


# ============================================================================
# Report Formatting (ANSI Terminal)
# ============================================================================

class Colors:
    """ANSI color codes."""
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"

    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    GRAY = "\033[90m"


def _section_header(title: str):
    """Print a section header."""
    print(f"\n{Colors.CYAN}── {title} {Colors.GRAY}{'-' * (50 - len(title))}{Colors.RESET}")


def _status_icon(passed: bool) -> str:
    """Return status icon."""
    return f"{Colors.GREEN}✓{Colors.RESET}" if passed else f"{Colors.RED}✗{Colors.RESET}"


def _pnl_color(value: float) -> str:
    """Return color for PnL value."""
    if value > 0:
        return Colors.GREEN
    elif value < 0:
        return Colors.RED
    else:
        return Colors.GRAY


def print_report_header(date_str: str, generated_at: str):
    """Print report header."""
    print(f"\n{Colors.BOLD}{Colors.CYAN}{'=' * 64}")
    print(f"NexusTrader — Daily Paper Trading Report")
    print(f"  Date: {date_str}  |  Generated: {generated_at} UTC")
    print(f"{'=' * 64}{Colors.RESET}\n")


def print_report_footer(trade_metrics: TradeMetrics):
    """Print report footer summary."""
    wr = trade_metrics.win_rate_pct
    pf = trade_metrics.profit_factor
    avg_r = trade_metrics.avg_r
    pnl = trade_metrics.total_pnl_usdt

    pnl_color = _pnl_color(pnl)

    summary = (
        f"{Colors.BOLD}{Colors.CYAN}{'=' * 64}{Colors.RESET}\n"
        f"{Colors.BOLD}SUMMARY: {trade_metrics.trades_closed} trades closed | "
        f"WR {wr:.1f}% | PF {pf:.2f} | AvgR {avg_r:+.2f} | "
        f"{pnl_color}PnL ${pnl:+,.2f}{Colors.RESET}{Colors.BOLD}\n"
        f"{'=' * 64}{Colors.RESET}\n"
    )
    print(summary)


def print_trade_activity(metrics: TradeMetrics):
    """Print trade activity section."""
    _section_header("Trade Activity")
    print(f"  Opened today:     {metrics.trades_opened}")
    print(f"  Closed today:     {metrics.trades_closed}")
    print(f"  Pending:          {metrics.trades_opened - metrics.trades_closed}")


def print_performance(metrics: TradeMetrics):
    """Print performance section."""
    _section_header("Performance (closed trades)")

    if metrics.trades_closed == 0:
        print("  [No closed trades yet]")
        return

    # Win Rate
    wr_threshold = 0.45
    wr_pass = metrics.win_rate_pct >= (wr_threshold * 100)
    print(f"  Win Rate:         {metrics.win_rate_pct:>6.1f}%   [threshold ≥ {wr_threshold*100:.0f}%]  {_status_icon(wr_pass)}")

    # Profit Factor
    pf_threshold = 1.10
    pf_pass = metrics.profit_factor >= pf_threshold
    pf_val = metrics.profit_factor if metrics.profit_factor != float('inf') else "∞"
    print(f"  Profit Factor:    {pf_val:>6}   [threshold ≥ {pf_threshold}]  {_status_icon(pf_pass)}")

    # Avg R
    avg_r_threshold = 0.10
    avg_r_pass = metrics.avg_r >= avg_r_threshold
    print(f"  Avg R/trade:      {metrics.avg_r:>+6.2f}   [threshold ≥ {avg_r_threshold}]  {_status_icon(avg_r_pass)}")

    # Expectancy
    exp_pass = metrics.expectancy_r > 0
    print(f"  Expectancy R:     {metrics.expectancy_r:>+6.2f}   {_status_icon(exp_pass)}")

    # Drawdown
    dd_threshold = 10.0
    dd_pass = metrics.max_drawdown_r <= dd_threshold
    print(f"  Max Drawdown R:   {metrics.max_drawdown_r:>6.2f}   [limit ≤ {dd_threshold}]  {_status_icon(dd_pass)}")

    # PnL
    pnl_color = _pnl_color(metrics.total_pnl_usdt)
    print(f"  Total PnL:        {pnl_color}${metrics.total_pnl_usdt:>+,.2f}{Colors.RESET}")

    print(f"  Avg Duration:     {metrics.avg_duration_h:>6.2f}h")
    print(f"  Win/Loss split:   {metrics.wins}/{metrics.losses}")


def print_by_symbol(metrics: TradeMetrics):
    """Print by-symbol breakdown."""
    if not metrics.by_symbol:
        return

    _section_header("By Symbol")
    for symbol in sorted(metrics.by_symbol.keys()):
        data = metrics.by_symbol[symbol]
        pnl_color = _pnl_color(data['pnl'])
        pf_val = data['pf'] if data['pf'] != float('inf') else "∞"
        print(f"  {symbol:>8}  {data['trades']:>2} trades  WR {data['wr']:>5.1f}%  PF {pf_val:>6}  {pnl_color}${data['pnl']:>+7,.0f}{Colors.RESET}")


def print_analysis_quality(analysis: AnalysisMetrics, trade_count: int):
    """Print analysis quality section."""
    _section_header("Trade Quality (analysis)")

    if trade_count == 0:
        print("  [No closed trades yet]")
        return

    if analysis.analysis_available == 0:
        print("  [unavailable — feedback not yet generated]")
        return

    cov_color = Colors.GREEN if analysis.analysis_coverage_pct >= 80 else Colors.YELLOW
    print(f"  Analysis coverage:  {cov_color}{analysis.analysis_available}/{trade_count} ({analysis.analysis_coverage_pct:.0f}%){Colors.RESET}")

    good_color = Colors.GREEN
    bad_color = Colors.RED
    neutral_color = Colors.GRAY

    print(f"  Classifications:    {good_color}GOOD {analysis.good_count}{Colors.RESET}  {bad_color}BAD {analysis.bad_count}{Colors.RESET}  {neutral_color}NEUTRAL {analysis.neutral_count}{Colors.RESET}")
    print(f"  Percentages:        {good_color}Good {analysis.good_pct:.0f}%{Colors.RESET}  {bad_color}Bad {analysis.bad_pct:.0f}%{Colors.RESET}  {neutral_color}Neutral {analysis.neutral_pct:.0f}%{Colors.RESET}")

    print(f"  Avg Quality Score:  {analysis.avg_overall_score:.2f}")
    if analysis.avoidable_loss_count > 0:
        print(f"  Avoidable Losses:   {bad_color}{analysis.avoidable_loss_count}{Colors.RESET} ({analysis.avoidable_loss_pct:.0f}%)")


def print_decision_matrix(analysis: AnalysisMetrics):
    """Print decision outcome matrix."""
    _section_header("Decision Matrix")

    total = (analysis.good_win_count + analysis.good_loss_count +
             analysis.bad_win_count + analysis.bad_loss_count +
             analysis.neutral_win_count + analysis.neutral_loss_count)

    if total == 0:
        print("  [No feedback available]")
        return

    print(f"  GOOD_WIN:          {analysis.good_win_count:>3}")
    print(f"  GOOD_LOSS:         {analysis.good_loss_count:>3}")
    print(f"  BAD_WIN:           {analysis.bad_win_count:>3}")
    print(f"  BAD_LOSS:          {analysis.bad_loss_count:>3}")
    print(f"  NEUTRAL_WIN:       {analysis.neutral_win_count:>3}")
    print(f"  NEUTRAL_LOSS:      {analysis.neutral_loss_count:>3}")


def print_root_causes(analysis: AnalysisMetrics):
    """Print top root causes."""
    _section_header("Root Causes (top 5)")

    if not analysis.top_root_causes:
        print("  [No root cause data available]")
        return

    for cause, count in analysis.top_root_causes:
        print(f"  {cause:40} × {count}")


def print_recommendations(analysis: AnalysisMetrics):
    """Print top recommendations."""
    _section_header("Recommendations (top 5)")

    if not analysis.top_recommendations:
        print("  [No recommendation data available]")
        return

    for rec, count in analysis.top_recommendations:
        print(f"  {rec:40} × {count}")


def print_proposals(proposals: ProposalMetrics):
    """Print proposal status."""
    _section_header("Strategy Tuning Proposals")

    total = (proposals.proposals_pending + proposals.proposals_approved +
             proposals.proposals_rejected + proposals.proposals_applied)

    if total == 0:
        print("  [No proposals yet]")
        return

    print(f"  Pending:           {proposals.proposals_pending:>3}")
    print(f"  Approved:          {proposals.proposals_approved:>3}")
    print(f"  Rejected:          {proposals.proposals_rejected:>3}")
    print(f"  Applied:           {proposals.proposals_applied:>3}")


def print_system_health(system: SystemMetrics):
    """Print system health metrics."""
    _section_header("System Health")

    total_analysis = system.analysis_ok + system.analysis_error + system.analysis_fallback
    total_notif = system.notif_ok + system.notif_error
    total_filter = system.filter_stats_ok + system.filter_stats_error

    if total_analysis > 0:
        analysis_health = Colors.GREEN if system.analysis_error == 0 else Colors.RED
        print(f"  Analysis:          {analysis_health}OK {system.analysis_ok}  ERROR {system.analysis_error}  FALLBACK {system.analysis_fallback}{Colors.RESET}")

    if total_notif > 0:
        notif_health = Colors.GREEN if system.notif_error == 0 else Colors.RED
        print(f"  Notifications:     {notif_health}OK {system.notif_ok}  ERROR {system.notif_error}{Colors.RESET}")

    if total_filter > 0:
        filter_health = Colors.GREEN if system.filter_stats_error == 0 else Colors.RED
        print(f"  Filter Stats:      {filter_health}OK {system.filter_stats_ok}  ERROR {system.filter_stats_error}{Colors.RESET}")

    if system.contract_violations > 0:
        print(f"  Contract Viol.:    {Colors.RED}{system.contract_violations}{Colors.RESET}")

    if total_analysis == 0 and total_notif == 0 and total_filter == 0:
        print("  [No system metrics available yet]")


def print_milestone_status(stats: dict, milestone_eval: dict):
    """Print milestone evaluation."""
    _section_header("Milestone Status")

    if not milestone_eval:
        print("  [Milestone evaluator not available]")
        return

    # Show status from evaluator
    status = milestone_eval.get('status', 'unknown')
    phase = milestone_eval.get('phase', 1)
    readiness = milestone_eval.get('readiness_pct', 0)

    status_color = Colors.GREEN if status == 'ready' else Colors.YELLOW if status == 'progress' else Colors.RED

    print(f"  Phase:             {phase}")
    print(f"  Status:            {status_color}{status}{Colors.RESET}")
    print(f"  Readiness:         {readiness:.0f}%")

    # Show detailed checks if available
    checks = milestone_eval.get('checks', {})
    if checks:
        _section_header("Milestone Checks")
        for check_name, check_result in checks.items():
            icon = _status_icon(check_result.get('passed', False))
            print(f"  {check_name:30}  {icon}  {check_result.get('message', '')}")


# ============================================================================
# HTML Output
# ============================================================================

def generate_html_report(
    date_str: str,
    trade_metrics: TradeMetrics,
    analysis_metrics: AnalysisMetrics,
    proposal_metrics: ProposalMetrics,
    system_metrics: SystemMetrics,
    milestone_eval: dict = None,
) -> str:
    """
    Generate HTML report as string.
    """
    html_parts = []

    html_parts.append("<!DOCTYPE html>")
    html_parts.append("<html>")
    html_parts.append("<head>")
    html_parts.append("  <meta charset='utf-8'>")
    html_parts.append(f"  <title>NexusTrader Daily Report - {date_str}</title>")
    html_parts.append("  <style>")
    html_parts.append("""
    body {
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        background: #1e1e1e;
        color: #e0e0e0;
        margin: 0;
        padding: 20px;
    }
    .container {
        max-width: 1000px;
        margin: 0 auto;
    }
    header {
        background: linear-gradient(135deg, #1a4a6b, #0d2b3e);
        color: white;
        padding: 30px;
        border-radius: 8px;
        margin-bottom: 30px;
        box-shadow: 0 4px 6px rgba(0,0,0,0.3);
    }
    header h1 {
        margin: 0 0 10px 0;
        font-size: 28px;
    }
    header p {
        margin: 5px 0;
        opacity: 0.9;
    }
    .section {
        background: #2d2d2d;
        padding: 20px;
        margin-bottom: 20px;
        border-radius: 6px;
        border-left: 4px solid #0096cc;
    }
    .section h2 {
        margin: 0 0 15px 0;
        color: #00d4ff;
        font-size: 18px;
        border-bottom: 1px solid #444;
        padding-bottom: 10px;
    }
    table {
        width: 100%;
        border-collapse: collapse;
        margin-top: 10px;
    }
    td {
        padding: 8px;
        border-bottom: 1px solid #444;
    }
    td:first-child {
        color: #aaa;
        width: 40%;
    }
    .metric-row {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 15px;
        margin-bottom: 10px;
    }
    .metric {
        background: #1e1e1e;
        padding: 12px;
        border-radius: 4px;
        border-left: 3px solid #444;
    }
    .metric-label {
        color: #999;
        font-size: 12px;
        text-transform: uppercase;
    }
    .metric-value {
        font-size: 24px;
        font-weight: bold;
        color: white;
        margin-top: 5px;
    }
    .positive {
        color: #4ade80;
    }
    .negative {
        color: #ff6b6b;
    }
    .neutral {
        color: #999;
    }
    .good { background-color: #1a3a2a; color: #4ade80; }
    .bad { background-color: #3a1a1a; color: #ff6b6b; }
    .neutral-cell { background-color: #2a2a2a; color: #999; }
    .check-pass { color: #4ade80; }
    .check-fail { color: #ff6b6b; }
    .summary {
        background: linear-gradient(135deg, #1a4a6b, #0d2b3e);
        padding: 20px;
        border-radius: 6px;
        margin-top: 30px;
    }
    .summary h3 {
        margin: 0 0 15px 0;
        color: #00d4ff;
    }
    """)
    html_parts.append("  </style>")
    html_parts.append("</head>")
    html_parts.append("<body>")

    html_parts.append("<div class='container'>")

    # Header
    now = datetime.utcnow().strftime("%H:%M")
    html_parts.append(f"""
    <header>
        <h1>NexusTrader — Daily Paper Trading Report</h1>
        <p><strong>Date:</strong> {date_str}</p>
        <p><strong>Generated:</strong> {now} UTC</p>
    </header>
    """)

    # Trade Activity
    html_parts.append("""
    <div class="section">
        <h2>Trade Activity</h2>
        <div class="metric-row">
            <div class="metric">
                <div class="metric-label">Opened</div>
                <div class="metric-value">{}</div>
            </div>
            <div class="metric">
                <div class="metric-label">Closed</div>
                <div class="metric-value">{}</div>
            </div>
        </div>
    </div>
    """.format(trade_metrics.trades_opened, trade_metrics.trades_closed))

    # Performance
    if trade_metrics.trades_closed > 0:
        wr_class = "positive" if trade_metrics.win_rate_pct >= 45 else "negative"
        pf_class = "positive" if trade_metrics.profit_factor >= 1.10 else "negative"
        exp_class = "positive" if trade_metrics.expectancy_r > 0 else "negative"
        pnl_class = "positive" if trade_metrics.total_pnl_usdt > 0 else ("negative" if trade_metrics.total_pnl_usdt < 0 else "neutral")

        html_parts.append(f"""
        <div class="section">
            <h2>Performance (closed trades)</h2>
            <div class="metric-row">
                <div class="metric">
                    <div class="metric-label">Win Rate</div>
                    <div class="metric-value {wr_class}">{trade_metrics.win_rate_pct:.1f}%</div>
                </div>
                <div class="metric">
                    <div class="metric-label">Profit Factor</div>
                    <div class="metric-value {pf_class}">{trade_metrics.profit_factor:.2f}</div>
                </div>
                <div class="metric">
                    <div class="metric-label">Avg R/trade</div>
                    <div class="metric-value {exp_class}">{trade_metrics.avg_r:+.2f}</div>
                </div>
                <div class="metric">
                    <div class="metric-label">Total PnL</div>
                    <div class="metric-value {pnl_class}">${trade_metrics.total_pnl_usdt:+,.2f}</div>
                </div>
            </div>
        </div>
        """)

    # By Symbol
    if trade_metrics.by_symbol:
        html_parts.append("""
        <div class="section">
            <h2>By Symbol</h2>
            <table>
                <tr>
                    <td><strong>Symbol</strong></td>
                    <td><strong>Trades</strong></td>
                    <td><strong>Win Rate</strong></td>
                    <td><strong>Profit Factor</strong></td>
                    <td><strong>PnL</strong></td>
                </tr>
        """)
        for symbol in sorted(trade_metrics.by_symbol.keys()):
            data = trade_metrics.by_symbol[symbol]
            pnl_class = "positive" if data['pnl'] > 0 else ("negative" if data['pnl'] < 0 else "neutral")
            html_parts.append(f"""
                <tr>
                    <td>{symbol}</td>
                    <td>{data['trades']}</td>
                    <td>{data['wr']:.1f}%</td>
                    <td>{data['pf']:.2f}</td>
                    <td class="{pnl_class}">${data['pnl']:+,.0f}</td>
                </tr>
            """)
        html_parts.append("            </table>")
        html_parts.append("        </div>")

    # Analysis Quality
    if analysis_metrics.analysis_available > 0:
        html_parts.append(f"""
        <div class="section">
            <h2>Trade Quality (analysis)</h2>
            <table>
                <tr>
                    <td>Coverage</td>
                    <td>{analysis_metrics.analysis_available}/{trade_metrics.trades_closed} ({analysis_metrics.analysis_coverage_pct:.0f}%)</td>
                </tr>
                <tr>
                    <td class="good">GOOD</td>
                    <td class="good">{analysis_metrics.good_count} ({analysis_metrics.good_pct:.0f}%)</td>
                </tr>
                <tr>
                    <td class="bad">BAD</td>
                    <td class="bad">{analysis_metrics.bad_count} ({analysis_metrics.bad_pct:.0f}%)</td>
                </tr>
                <tr>
                    <td class="neutral-cell">NEUTRAL</td>
                    <td class="neutral-cell">{analysis_metrics.neutral_count} ({analysis_metrics.neutral_pct:.0f}%)</td>
                </tr>
                <tr>
                    <td>Avg Quality Score</td>
                    <td>{analysis_metrics.avg_overall_score:.2f}</td>
                </tr>
            </table>
        </div>
        """)

    # Decision Matrix
    total_decisions = (analysis_metrics.good_win_count + analysis_metrics.good_loss_count +
                      analysis_metrics.bad_win_count + analysis_metrics.bad_loss_count +
                      analysis_metrics.neutral_win_count + analysis_metrics.neutral_loss_count)
    if total_decisions > 0:
        html_parts.append(f"""
        <div class="section">
            <h2>Decision Matrix</h2>
            <table>
                <tr class="good"><td>GOOD_WIN</td><td>{analysis_metrics.good_win_count}</td></tr>
                <tr class="good"><td>GOOD_LOSS</td><td>{analysis_metrics.good_loss_count}</td></tr>
                <tr class="bad"><td>BAD_WIN</td><td>{analysis_metrics.bad_win_count}</td></tr>
                <tr class="bad"><td>BAD_LOSS</td><td>{analysis_metrics.bad_loss_count}</td></tr>
                <tr class="neutral-cell"><td>NEUTRAL_WIN</td><td>{analysis_metrics.neutral_win_count}</td></tr>
                <tr class="neutral-cell"><td>NEUTRAL_LOSS</td><td>{analysis_metrics.neutral_loss_count}</td></tr>
            </table>
        </div>
        """)

    # Root Causes
    if analysis_metrics.top_root_causes:
        html_parts.append("""
        <div class="section">
            <h2>Top Root Causes</h2>
            <table>
        """)
        for cause, count in analysis_metrics.top_root_causes:
            html_parts.append(f"<tr><td>{cause}</td><td>× {count}</td></tr>")
        html_parts.append("            </table>")
        html_parts.append("        </div>")

    # Recommendations
    if analysis_metrics.top_recommendations:
        html_parts.append("""
        <div class="section">
            <h2>Top Recommendations</h2>
            <table>
        """)
        for rec, count in analysis_metrics.top_recommendations:
            html_parts.append(f"<tr><td>{rec}</td><td>× {count}</td></tr>")
        html_parts.append("            </table>")
        html_parts.append("        </div>")

    # Proposals
    total_props = (proposal_metrics.proposals_pending + proposal_metrics.proposals_approved +
                  proposal_metrics.proposals_rejected + proposal_metrics.proposals_applied)
    if total_props > 0:
        html_parts.append(f"""
        <div class="section">
            <h2>Strategy Tuning Proposals</h2>
            <table>
                <tr><td>Pending</td><td>{proposal_metrics.proposals_pending}</td></tr>
                <tr><td>Approved</td><td>{proposal_metrics.proposals_approved}</td></tr>
                <tr><td>Rejected</td><td>{proposal_metrics.proposals_rejected}</td></tr>
                <tr><td>Applied</td><td>{proposal_metrics.proposals_applied}</td></tr>
            </table>
        </div>
        """)

    # System Health
    html_parts.append("""
    <div class="section">
        <h2>System Health</h2>
        <table>
    """)
    if system_metrics.analysis_ok + system_metrics.analysis_error + system_metrics.analysis_fallback > 0:
        html_parts.append(f"<tr><td>Analysis</td><td><span class='check-pass'>OK {system_metrics.analysis_ok}</span> | ERROR {system_metrics.analysis_error} | FALLBACK {system_metrics.analysis_fallback}</td></tr>")
    if system_metrics.notif_ok + system_metrics.notif_error > 0:
        notif_class = "check-pass" if system_metrics.notif_error == 0 else "check-fail"
        html_parts.append(f"<tr><td>Notifications</td><td><span class='{notif_class}'>OK {system_metrics.notif_ok}</span> | <span class='check-fail'>ERROR {system_metrics.notif_error}</span></td></tr>")
    if system_metrics.filter_stats_ok + system_metrics.filter_stats_error > 0:
        filter_class = "check-pass" if system_metrics.filter_stats_error == 0 else "check-fail"
        html_parts.append(f"<tr><td>Filter Stats</td><td><span class='{filter_class}'>OK {system_metrics.filter_stats_ok}</span> | <span class='check-fail'>ERROR {system_metrics.filter_stats_error}</span></td></tr>")
    if system_metrics.contract_violations > 0:
        html_parts.append(f"<tr><td>Contract Violations</td><td><span class='check-fail'>{system_metrics.contract_violations}</span></td></tr>")
    html_parts.append("        </table>")
    html_parts.append("    </div>")

    # Milestone
    if milestone_eval:
        phase = milestone_eval.get('phase', 1)
        status = milestone_eval.get('status', 'unknown')
        readiness = milestone_eval.get('readiness_pct', 0)
        status_class = "check-pass" if status == 'ready' else "check-fail"
        html_parts.append(f"""
        <div class="section">
            <h2>Milestone Status</h2>
            <table>
                <tr><td>Phase</td><td>{phase}</td></tr>
                <tr><td>Status</td><td><span class='{status_class}'>{status.upper()}</span></td></tr>
                <tr><td>Readiness</td><td>{readiness:.0f}%</td></tr>
            </table>
        </div>
        """)

    # Summary
    wr = trade_metrics.win_rate_pct
    pf = trade_metrics.profit_factor
    avg_r = trade_metrics.avg_r
    pnl = trade_metrics.total_pnl_usdt
    pnl_class = "positive" if pnl > 0 else ("negative" if pnl < 0 else "neutral")

    html_parts.append(f"""
    <div class="summary">
        <h3>Summary</h3>
        <p><strong>{trade_metrics.trades_closed}</strong> trades closed |
           <strong>WR {wr:.1f}%</strong> |
           <strong>PF {pf:.2f}</strong> |
           <strong>AvgR {avg_r:+.2f}</strong> |
           <strong class="{pnl_class}">PnL ${pnl:+,.2f}</strong></p>
    </div>
    """)

    html_parts.append("</div>")  # container
    html_parts.append("</body>")
    html_parts.append("</html>")

    return "\n".join(html_parts)


# ============================================================================
# Main
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="NexusTrader Daily Paper Trading Report"
    )
    parser.add_argument(
        "--date",
        type=str,
        help="Specific date (ISO format, e.g. 2026-03-25)"
    )
    parser.add_argument(
        "--all-time",
        action="store_true",
        help="Report on all trades ever (for milestone checks)"
    )
    parser.add_argument(
        "--last-n",
        type=int,
        help="Report on last N days (e.g. 7)"
    )
    parser.add_argument(
        "--html",
        action="store_true",
        help="Also save HTML report to reports/daily_{date}.html"
    )

    args = parser.parse_args()

    # Determine date range
    date_start, date_end = parse_date_range(args)

    # For display
    if args.all_time:
        report_date = "all-time"
    elif args.last_n:
        today = datetime.utcnow().date()
        start = today - timedelta(days=args.last_n - 1)
        report_date = f"{start} to {today}"
    elif args.date:
        report_date = args.date
    else:
        report_date = datetime.utcnow().date().isoformat()

    generated_at = datetime.utcnow().strftime("%H:%M:%S")

    # Print header
    print_report_header(report_date, generated_at)

    # Load data from database and compute metrics inside session context.
    # ORM objects become detached (and unrefreshable) once the session closes,
    # so all attribute access must complete before the `with` block exits.
    trade_metrics = None
    analysis_metrics = None
    proposal_metrics = None
    all_trade_metrics = None
    all_analysis_metrics = None
    try:
        with _get_session() as session:
            # Query trades and feedback
            trades = _get_paper_trades(session, date_start, date_end)
            feedbacks = _get_trade_feedback(session, date_start, date_end)
            proposals = _get_proposals(session)

            # For all-time milestone check
            all_trades = _get_paper_trades(session, None, None)

            # Compute all metrics while session is still open so ORM attribute
            # access (e.g. t.closed_at) does not trigger DetachedInstanceError.
            trade_metrics = compute_trade_metrics(trades)
            analysis_metrics = compute_analysis_metrics(trades, feedbacks)
            proposal_metrics = compute_proposal_metrics(proposals)
            all_trade_metrics = compute_trade_metrics(all_trades)
            all_analysis_metrics = compute_analysis_metrics(all_trades, feedbacks)
    except Exception as e:
        print(f"Error loading database: {e}", file=sys.stderr)

    # Provide safe defaults if DB load/compute failed
    if trade_metrics is None:
        trade_metrics = compute_trade_metrics([])
    if analysis_metrics is None:
        analysis_metrics = compute_analysis_metrics([], [])
    if proposal_metrics is None:
        proposal_metrics = compute_proposal_metrics([])
    if all_trade_metrics is None:
        all_trade_metrics = compute_trade_metrics([])
    if all_analysis_metrics is None:
        all_analysis_metrics = compute_analysis_metrics([], [])

    snapshot = _get_analysis_metrics_snapshot()
    system_metrics = compute_system_metrics(snapshot)

    stats = {
        "total_trades": all_trade_metrics.trades_closed,
        "win_rate_pct": all_trade_metrics.win_rate_pct,
        "profit_factor": all_trade_metrics.profit_factor,
        "expectancy_r": all_trade_metrics.expectancy_r,
        "avg_r": all_trade_metrics.avg_r,
        "good_trade_pct": all_analysis_metrics.good_pct,
        "bad_trade_pct": all_analysis_metrics.bad_pct,
        "neutral_trade_pct": all_analysis_metrics.neutral_pct,
        "drawdown_r": all_trade_metrics.max_drawdown_r,
        # analysis_success_rate: percentage scale (0–100) to match MILESTONES thresholds.
        # LiveReadinessEvaluator normalises to fraction internally when comparing 0–1 gates.
        "analysis_success_rate": all_analysis_metrics.analysis_coverage_pct if all_analysis_metrics.analysis_coverage_pct else 0.0,
        "notification_reliability": (system_metrics.notif_ok / (system_metrics.notif_ok + system_metrics.notif_error)) if (system_metrics.notif_ok + system_metrics.notif_error) > 0 else 0.0,
        "bad_decision_pct": all_analysis_metrics.bad_pct,
        "avoidable_loss_pct": all_analysis_metrics.avoidable_loss_pct,
    }

    milestone_eval = evaluate_milestone(stats)

    # Print sections
    print_trade_activity(trade_metrics)
    print_performance(trade_metrics)
    print_by_symbol(trade_metrics)
    print_analysis_quality(analysis_metrics, trade_metrics.trades_closed)
    print_decision_matrix(analysis_metrics)
    print_root_causes(analysis_metrics)
    print_recommendations(analysis_metrics)
    print_proposals(proposal_metrics)
    print_system_health(system_metrics)
    print_milestone_status(stats, milestone_eval)

    # Print footer
    print_report_footer(trade_metrics)

    # HTML output if requested
    if args.html:
        try:
            reports_dir = _PROJECT_ROOT / "reports"
            reports_dir.mkdir(exist_ok=True)

            html_filename = f"daily_{report_date.replace(' to ', '_to_')}.html"
            html_path = reports_dir / html_filename

            html_content = generate_html_report(
                report_date,
                trade_metrics,
                analysis_metrics,
                proposal_metrics,
                system_metrics,
                milestone_eval
            )

            with open(html_path, 'w') as f:
                f.write(html_content)

            print(f"\n{Colors.GREEN}HTML report saved to: {html_path}{Colors.RESET}")
        except Exception as e:
            print(f"\n{Colors.RED}Error writing HTML report: {e}{Colors.RESET}", file=sys.stderr)


if __name__ == "__main__":
    main()
