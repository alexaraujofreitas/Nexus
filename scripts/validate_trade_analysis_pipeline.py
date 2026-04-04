#!/usr/bin/env python3
"""
NexusTrader — Trade Analysis Pipeline Readiness Validator
=========================================================
Runs a comprehensive PASS/FAIL check across the full Phase 3
trade-analysis subsystem. Safe to run at any time — no side
effects, no DB writes, no network calls.

Usage:
    python scripts/validate_trade_analysis_pipeline.py
    python scripts/validate_trade_analysis_pipeline.py --verbose

Exit code: 0 = all PASS, 1 = any FAIL.
"""
from __future__ import annotations

import argparse
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

# ── Ensure project root is on sys.path ────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ── Colour codes (ANSI) ───────────────────────────────────────
_GREEN  = "\033[92m"
_RED    = "\033[91m"
_YELLOW = "\033[93m"
_CYAN   = "\033[96m"
_BOLD   = "\033[1m"
_RESET  = "\033[0m"

_results: list[tuple[str, bool, str]] = []   # (check_name, passed, detail)
_verbose = False


def _check(name: str, passed: bool, detail: str = "") -> None:
    _results.append((name, passed, detail))
    icon = f"{_GREEN}PASS{_RESET}" if passed else f"{_RED}FAIL{_RESET}"
    line = f"  {icon}  {name}"
    if detail and (_verbose or not passed):
        line += f"\n         {_YELLOW}{detail}{_RESET}"
    print(line)


def _section(title: str) -> None:
    print(f"\n{_BOLD}{_CYAN}── {title} {'─' * max(0, 55 - len(title))}{_RESET}")


# ── Sample data ───────────────────────────────────────────────

_OPEN_TRADE = {
    "symbol": "BTCUSDT", "side": "buy",
    "entry_price": 67500.0, "current_price": 68200.0,
    "stop_loss": 66500.0, "take_profit": 70000.0,
    "size_usdt": 1350.0, "regime": "bull_trend",
    "models_fired": ["TrendModel", "MomentumBreakout"],
    "score": 0.71, "htf_regime": "bull_trend",
    "regime_confidence": 0.78, "signal_conflict_score": 0.12,
    "atr": 900.0, "opened_at": "2026-03-25T10:00:00Z",
}

_CLOSED_TRADE = {
    "symbol": "ETHUSDT", "side": "buy",
    "entry_price": 3500.0, "exit_price": 3420.0,
    "stop_loss": 3400.0, "take_profit": 3800.0,
    "pnl_usdt": -95.0, "pnl_pct": -0.023,
    "size_usdt": 1200.0, "regime": "bull_trend",
    "models_fired": ["TrendModel", "MomentumBreakout"],
    "score": 0.68, "htf_regime": "bull_trend",
    "regime_confidence": 0.75, "signal_conflict_score": 0.10,
    "atr": 80.0, "exit_reason": "stop_loss",
    "duration_s": 3600, "opened_at": "2026-03-24T14:00:00Z",
}


def check_migrations() -> None:
    _section("1. Schema Migrations")
    try:
        from core.database.engine import engine, get_session
        from sqlalchemy import text
        with get_session() as session:
            # Phase 2 TradeFeedback columns
            phase2_cols = ["decision_outcome_matrix", "avoidable_loss_flag",
                           "preventability_score", "randomness_score",
                           "regime_confidence_at_entry", "signal_conflict_score"]
            for col in phase2_cols:
                try:
                    session.execute(text(f"SELECT {col} FROM trade_feedback LIMIT 1"))
                    _check(f"trade_feedback.{col}", True)
                except Exception as e:
                    _check(f"trade_feedback.{col}", False, str(e))
            # New tables
            for table in ("strategy_tuning_proposals", "applied_strategy_changes"):
                try:
                    session.execute(text(f"SELECT COUNT(*) FROM {table}"))
                    _check(f"Table: {table}", True)
                except Exception as e:
                    _check(f"Table: {table}", False, str(e))
    except Exception as e:
        _check("Database connection", False, str(e))


def check_canonical_service() -> None:
    _section("2. Canonical Service")
    try:
        from core.analysis.trade_analysis_service import trade_analysis_service
        _check("trade_analysis_service importable", True)

        analysis = trade_analysis_service.build_trade_analysis(
            _OPEN_TRADE,
            current_regime="bull_trend", current_confluence=0.71,
        )
        _check("build_open_trade_analysis()", bool(analysis.get("classification")),
               f"classification={analysis.get('classification')}")

        analysis_closed = trade_analysis_service.build_trade_analysis(_CLOSED_TRADE)
        _check("build_closed_trade_analysis()", bool(analysis_closed.get("forensics")),
               f"matrix={analysis_closed.get('forensics', {}).get('decision_outcome_matrix_label')}")

    except Exception as e:
        _check("Canonical service", False, traceback.format_exc(limit=2))


def check_contract_validation() -> None:
    _section("3. Contract Validation")
    try:
        from core.analysis.analysis_contract import (
            validate_open_analysis, validate_closed_analysis, VERSION
        )
        from core.analysis.trade_analysis_service import trade_analysis_service

        open_a  = trade_analysis_service.build_trade_analysis(_OPEN_TRADE,
                      current_regime="bull_trend", current_confluence=0.71)
        closed_a = trade_analysis_service.build_trade_analysis(_CLOSED_TRADE)

        open_errors   = validate_open_analysis(open_a)
        closed_errors = validate_closed_analysis(closed_a)

        _check("Open analysis contract valid",   not open_errors,
               "; ".join(open_errors) if open_errors else "")
        _check("Closed analysis contract valid", not closed_errors,
               "; ".join(closed_errors) if closed_errors else "")
        _check(f"Contract version={VERSION} stamped",
               open_a.get("_contract_version") == VERSION,
               f"got {open_a.get('_contract_version')!r}")
    except Exception:
        _check("Contract validation", False, traceback.format_exc(limit=2))


def check_renderers() -> None:
    _section("4. Canonical Renderers")
    try:
        from core.analysis.canonical_renderer import (
            render_for_channel, MODE_UI_OPEN, MODE_UI_CLOSED,
            MODE_NOTIF_OPEN, MODE_NOTIF_CLOSED, MODE_EMAIL_OPEN, MODE_EMAIL_CLOSED,
        )
        from core.analysis.trade_analysis_service import trade_analysis_service

        open_a  = trade_analysis_service.build_trade_analysis(_OPEN_TRADE,
                      current_regime="bull_trend", current_confluence=0.71)
        closed_a = trade_analysis_service.build_trade_analysis(_CLOSED_TRADE)

        modes = [
            (MODE_UI_OPEN,      _OPEN_TRADE,   open_a,   "8 sections"),
            (MODE_UI_CLOSED,    _CLOSED_TRADE, closed_a, "9 sections"),
            (MODE_NOTIF_OPEN,   _OPEN_TRADE,   open_a,   "notification"),
            (MODE_NOTIF_CLOSED, _CLOSED_TRADE, closed_a, "notification"),
            (MODE_EMAIL_OPEN,   _OPEN_TRADE,   open_a,   "email"),
            (MODE_EMAIL_CLOSED, _CLOSED_TRADE, closed_a, "email"),
        ]
        for mode, trade, analysis, label in modes:
            try:
                rendered = render_for_channel(analysis, mode, trade)
                ok = bool(rendered.get("classification"))
                _check(f"render_for_channel({mode[:12]})", ok, label)
            except Exception as e:
                _check(f"render_for_channel({mode[:12]})", False, str(e))
    except Exception:
        _check("Renderers importable", False, traceback.format_exc(limit=2))


def check_notification_projections() -> None:
    _section("5. Notification Projections")
    try:
        from core.analysis.trade_analysis_service import trade_analysis_service
        from core.analysis.analysis_contract import validate_notification_payload

        trade    = _OPEN_TRADE
        analysis = trade_analysis_service.build_trade_analysis(trade,
                       current_regime="bull_trend", current_confluence=0.71)
        payload  = trade_analysis_service.generate_notification_payload(trade, analysis)

        errors = validate_notification_payload(payload)
        _check("Notification payload valid", not errors,
               "; ".join(errors) if errors else "")

        lines = payload.get("analysis_notification_lines", [])
        _check("Notification lines non-empty", len(lines) >= 1,
               f"got {len(lines)} lines")

        # Check channel consistency
        from core.analysis.canonical_renderer import render_for_channel, MODE_NOTIF_OPEN
        rendered = render_for_channel(analysis, MODE_NOTIF_OPEN, trade)
        _check("classification consistent with canonical",
               rendered["classification"] == analysis["classification"])

    except Exception:
        _check("Notification projections", False, traceback.format_exc(limit=2))


def check_feedback_persistence() -> None:
    _section("6. Feedback Persistence")
    try:
        from core.analysis.trade_feedback_store import persist_trade_feedback
        _check("persist_trade_feedback importable", True)

        from core.analysis.adaptive_learning import full_feedback_summary
        summary = full_feedback_summary()
        _check("full_feedback_summary() callable",
               isinstance(summary, dict),
               f"total={summary.get('total', '?')}")
    except Exception as e:
        _check("Feedback persistence", False, str(e))


def check_proposal_generation() -> None:
    _section("7. Proposal Generation")
    try:
        from core.analysis.tuning_proposal_generator import generate_tuning_proposals
        _check("tuning_proposal_generator importable", True)

        summary = {
            "total": 15,
            "top_root_causes": [
                {
                    "category": "COUNTER_REGIME_ENTRY",
                    "count": 5, "pct": 33.0, "severity": "major",
                    "avg_overall_score": 42.0, "avg_pnl_usdt": -25.0,
                    "affected_subsystem": "SignalGenerator",
                },
            ],
            "top_recommendations": [],
        }
        proposals = generate_tuning_proposals(summary, min_trade_count=5,
                                              min_occurrence_pct=20.0)
        _check("generate_tuning_proposals() works", len(proposals) >= 1,
               f"generated {len(proposals)} proposals")

        from core.analysis.backtest_gating import load_pending_proposals
        pending = load_pending_proposals()
        _check("load_pending_proposals() callable",
               isinstance(pending, list),
               f"{len(pending)} pending in DB")
    except Exception as e:
        _check("Proposal generation", False, str(e))


def check_backtest_runner() -> None:
    _section("8. Backtest Runner")
    try:
        from core.analysis.backtest_runner import run_proposal_backtest, BacktestRunnerError
        _check("backtest_runner importable", True)

        # Test: missing parquet → ERROR (not crash)
        result = run_proposal_backtest(
            {"proposal_id": "val_test", "tuning_parameter": "min_confluence_score",
             "tuning_direction": "increase", "auto_tune_eligible": False},
            symbol="FAKECOIN", timeframe="1h",
        )
        _check("Missing parquet → ERROR (not crash)",
               result["gating_result"] == "ERROR",
               result.get("error", "")[:60])

        # Test dry-run
        result_dry = run_proposal_backtest(
            {"proposal_id": "val_dry", "tuning_parameter": "min_confluence_score",
             "tuning_direction": "increase", "auto_tune_eligible": False},
            dry_run=True,
        )
        _check("Dry-run returns APPROVE",
               result_dry["gating_result"] == "APPROVE",
               f"dry_run={result_dry.get('dry_run')}")

        # Test with real parquet if available
        from pathlib import Path
        parquet = Path("data/validation/BTCUSDT_1h.parquet")
        if parquet.exists():
            _check("Real BTCUSDT 1h parquet available",    True, str(parquet))
        else:
            _check("Real BTCUSDT 1h parquet available", False,
                   "Run scripts/fetch_historical_data_v2.py --resume")
    except Exception:
        _check("Backtest runner", False, traceback.format_exc(limit=2))


def check_proposal_review_workflow() -> None:
    _section("9. Proposal Review Workflow")
    try:
        from core.analysis.backtest_gating import (
            evaluate_proposal_vs_baseline, load_applied_changes,
        )
        _check("evaluate_proposal_vs_baseline importable", True)
        _check("load_applied_changes importable", True)

        result = evaluate_proposal_vs_baseline(
            {"proposal_id": "val_gate", "auto_tune_eligible": True},
            {"baseline_pf":    1.40, "proposed_pf":    1.52,
             "baseline_wr":   52.0, "proposed_wr":   54.0,
             "baseline_trades": 25, "proposed_trades": 25},
        )
        _check("APPROVE on PF improvement >= 2%",
               result["decision"] == "APPROVE",
               f"decision={result['decision']} delta_pf={result.get('delta_pf', 0):.2f}%")

        applied = load_applied_changes()
        _check("load_applied_changes() returns list", isinstance(applied, list),
               f"{len(applied)} applied changes in DB")

        # GUI page importable
        try:
            import gui.pages.proposals.proposals_page as _pp
            _check("ProposalsPage GUI importable", hasattr(_pp, "ProposalsPage"))
        except ImportError as e:
            _check("ProposalsPage GUI importable", False, str(e))

    except Exception:
        _check("Proposal review workflow", False, traceback.format_exc(limit=2))


def check_audit_trail() -> None:
    _section("10. Audit Trail")
    try:
        from core.analysis.backtest_gating import record_applied_change
        _check("record_applied_change importable", True)

        from core.analysis.analysis_metrics import (
            inc, get, snapshot, C_APPLIED_CHANGE
        )
        _check("analysis_metrics importable", True)

        inc(C_APPLIED_CHANGE)
        _check("analysis_metrics.inc() works", get(C_APPLIED_CHANGE) >= 1)

    except Exception as e:
        _check("Audit trail", False, str(e))


def check_filter_stats_wiring() -> None:
    _section("11. FilterStatsTracker Wiring")
    try:
        from core.analytics.filter_stats import get_filter_stats_tracker, FilterStatsTracker
        tracker = get_filter_stats_tracker()
        _check("get_filter_stats_tracker() importable", True)

        tracker.record_trade_outcome("time_of_day", 1.5)
        tracker.record_trade_outcome("volatility", -0.5)
        _check("record_trade_outcome() works for both filters", True)

        import inspect
        import core.execution.paper_executor as pe_mod
        src = inspect.getsource(pe_mod.PaperExecutor._close_position)
        wired = "filter_stats" in src or "get_filter_stats_tracker" in src
        _check("FilterStatsTracker wired into _close_position()", wired)

    except Exception as e:
        _check("Filter stats wiring", False, str(e))


def check_test_position_removed() -> None:
    _section("12. Test Position Button Removal")
    try:
        # Read source directly to avoid triggering Qt/libEGL at import time
        src_path = _PROJECT_ROOT / "gui" / "pages" / "orders_positions" / "orders_page.py"
        if not src_path.exists():
            _check("Test Position button check", False, f"Source not found: {src_path}")
            return
        src = src_path.read_text(encoding="utf-8")
        lines = src.split("\n")
        # Active if addWidget call references both "test" and "position" on same non-comment line
        active_button = any(
            "addWidget" in line and "test" in line.lower() and "position" in line.lower()
            for line in lines if not line.strip().startswith("#")
        )
        _check("Test Position button NOT active in layout",
               not active_button,
               "Button addWidget() still present — hide before release" if active_button else "")
        # Confirm the button is not forcibly shown
        exposed = any(
            ("test" in line.lower() and "position" in line.lower() and
             ("setVisible(True)" in line or ".show()" in line))
            for line in lines if not line.strip().startswith("#")
        )
        _check("Test Position button not forcibly shown",
               not exposed,
               "setVisible(True) / show() on test-position button found" if exposed else "")
    except Exception as e:
        _check("Test Position button check", False, str(e))


# ── Main ───────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="NexusTrader trade-analysis pipeline validator")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    global _verbose
    _verbose = args.verbose

    print(f"\n{_BOLD}NexusTrader — Trade Analysis Pipeline Readiness Check{_RESET}")
    print(f"  Version: Phase 3  |  Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 62)

    check_migrations()
    check_canonical_service()
    check_contract_validation()
    check_renderers()
    check_notification_projections()
    check_feedback_persistence()
    check_proposal_generation()
    check_backtest_runner()
    check_proposal_review_workflow()
    check_audit_trail()
    check_filter_stats_wiring()
    check_test_position_removed()

    passed = sum(1 for _, ok, _ in _results if ok)
    failed = len(_results) - passed

    print(f"\n{'=' * 62}")
    print(f"{_BOLD}RESULT: {passed} PASS  |  {failed} FAIL  |  {len(_results)} total{_RESET}")
    if failed == 0:
        print(f"{_GREEN}{_BOLD}OK — pipeline is operational{_RESET}\n")
    else:
        print(f"{_RED}{_BOLD}{failed} check(s) failed — see details above{_RESET}\n")
        print("Failed checks:")
        for name, ok, detail in _results:
            if not ok:
                print(f"  • {name}: {detail[:80] if detail else 'no detail'}")
        print()

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
