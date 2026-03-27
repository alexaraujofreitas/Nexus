#!/usr/bin/env python3
# ============================================================
# NEXUS TRADER v1.2 — Parity Validation Script
#
# Purpose : Verify that the live v1.2 demo configuration is
#           consistent with Phase 5 backtest expectations.
#           Run before every major demo session restart.
#
# Usage   : python scripts/validate_v1_2_parity.py
#           python scripts/validate_v1_2_parity.py --strict
#           python scripts/validate_v1_2_parity.py --json-output
#
# Exit codes:
#   0 — all checks passed (READY)
#   1 — one or more FAIL checks (NOT READY)
#   2 — one or more WARN checks, no FAIL (PROCEED WITH CAUTION)
# ============================================================
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# ── Bootstrap path so we can import project modules ──────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from config.settings import settings
    _SETTINGS_OK = True
except Exception as e:
    _SETTINGS_OK = False
    _SETTINGS_ERR = str(e)

try:
    from config.constants import APP_VERSION
    _CONST_OK = True
except Exception as e:
    _CONST_OK = False
    APP_VERSION = "unknown"

# ── Result tracking ───────────────────────────────────────────
_results: list[dict[str, Any]] = []

PASS  = "PASS"
WARN  = "WARN"
FAIL  = "FAIL"


def check(name: str, status: str, actual: Any, expected: Any, note: str = "") -> None:
    _results.append({"name": name, "status": status,
                     "actual": str(actual), "expected": str(expected), "note": note})


# ── ANSI colours ──────────────────────────────────────────────
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

STATUS_COLOR = {PASS: GREEN, WARN: YELLOW, FAIL: RED}


# ─────────────────────────────────────────────────────────────
# 1. VERSION
# ─────────────────────────────────────────────────────────────
def validate_version() -> None:
    print(f"\n{BOLD}[1] Version{RESET}")
    if not _CONST_OK:
        check("constants.py importable", FAIL, "ImportError", "importable")
        return
    status = PASS if APP_VERSION == "1.2.0" else FAIL
    check("APP_VERSION", status, APP_VERSION, "1.2.0")


# ─────────────────────────────────────────────────────────────
# 2. CONFIG PARAMETERS (Phase 5 winning config)
# ─────────────────────────────────────────────────────────────
def validate_config() -> None:
    print(f"\n{BOLD}[2] Config Parameters{RESET}")
    if not _SETTINGS_OK:
        check("config.yaml loadable", FAIL, f"ImportError: {_SETTINGS_ERR}", "loadable")
        return

    # ── Timeframe ──
    tf = settings.get("data.default_timeframe", "1h")
    check("default_timeframe", PASS if tf == "30m" else FAIL, tf, "30m",
          "Phase 5 winning: 30m primary TF")

    # ── ADX threshold ──
    adx = float(settings.get("models.trend.adx_min", 25.0))
    status = PASS if 30 <= adx <= 32 else (WARN if 28 <= adx <= 33 else FAIL)
    check("models.trend.adx_min", status, adx, "31 (range 30–32)",
          "Phase 5 lever 2: adx_min=31 → +42% trade count, no PF degradation")

    # ── Confluence threshold ──
    conf = float(settings.get("idss.min_confluence_score", 0.55))
    status = PASS if 0.44 <= conf <= 0.46 else (WARN if 0.40 <= conf <= 0.50 else FAIL)
    check("idss.min_confluence_score", status, conf, "0.45 (range 0.44–0.46)",
          "Phase 5 lever 2: thresh=0.45 optimal (PF=2.695)")

    # ── Exit mode ──
    exit_mode = settings.get("exit.mode", "full")
    check("exit.mode", PASS if exit_mode == "partial" else FAIL, exit_mode, "partial",
          "Phase 5 lever 1: partial exit → PF 1.825 → 2.634 (+44.6%)")

    # ── Partial exit percentage ──
    partial_pct = float(settings.get("exit.partial_pct", 0.0))
    status = PASS if 0.30 <= partial_pct <= 0.35 else (WARN if 0.25 <= partial_pct <= 0.40 else FAIL)
    check("exit.partial_pct", status, partial_pct, "0.33 (range 0.30–0.35)",
          "Phase 5: close 33% at 1R trigger")

    # ── Partial R trigger ──
    r_trigger = float(settings.get("exit.partial_r_trigger", 0.0))
    check("exit.partial_r_trigger", PASS if r_trigger == 1.0 else WARN, r_trigger, "1.0",
          "Partial exit fires when unrealised P&L = +1R")

    # ── Risk per trade ──
    risk_pct = float(settings.get("risk_engine.risk_pct_per_trade", 0.75))
    status = PASS if risk_pct == 0.5 else (WARN if risk_pct <= 1.0 else FAIL)
    check("risk_engine.risk_pct_per_trade", status, risk_pct, "0.5",
          "Phase 1 demo sizing (0.5%)")

    # ── Max capital per trade ──
    max_cap = float(settings.get("risk_engine.max_capital_pct", 0.25))
    status = PASS if max_cap == 0.04 else (WARN if max_cap <= 0.10 else FAIL)
    check("risk_engine.max_capital_pct", status, max_cap, "0.04 (4%)",
          "Hard cap per-trade; overrides Kelly sizing")

    # ── Auto-execute ──
    auto_exec = settings.get("scanner.auto_execute", False)
    check("scanner.auto_execute", PASS if auto_exec else FAIL, auto_exec, True,
          "Must be True — scanner fires automatically on every restart")

    # ── MTF confirmation ──
    mtf_req = settings.get("multi_tf.confirmation_required", False)
    check("multi_tf.confirmation_required", PASS if mtf_req else WARN, mtf_req, True,
          "Phase 5 lever 6: 30m+4h MTF gate (PF=2.976 vs 2.695 single-TF)")

    # ── WS disabled (stability) ──
    ws = settings.get("data.websocket_enabled", True)
    check("data.websocket_enabled", PASS if not ws else WARN, ws, False,
          "WS crashes Qt at 10Hz without throttle — use REST polling")

    # ── Disabled models ──
    disabled = set(settings.get("disabled_models", []))
    expected_disabled = {"mean_reversion", "liquidity_sweep"}
    missing_disabled = expected_disabled - disabled
    if not missing_disabled:
        check("disabled_models (required)", PASS, sorted(disabled & expected_disabled),
              sorted(expected_disabled))
    else:
        check("disabled_models (required)", FAIL,
              f"missing: {sorted(missing_disabled)}",
              f"both {sorted(expected_disabled)} must be disabled",
              "Study 4: PF<0.30 on both, combined -$33k")

    # ── RL shadow only ──
    rl_shadow = settings.get("rl.shadow_only", False)
    check("rl.shadow_only", PASS if rl_shadow else WARN, rl_shadow, True,
          "RL ensemble in observation-only mode until ≥75 live trades validated")


# ─────────────────────────────────────────────────────────────
# 3. SIGNAL GENERATOR — active models
# ─────────────────────────────────────────────────────────────
def validate_signal_generator() -> None:
    print(f"\n{BOLD}[3] Signal Generator — Active Models{RESET}")
    try:
        from core.signals.signal_generator import _ALL_MODELS
        active_names = {m.name for m in _ALL_MODELS}
    except Exception as exc:
        check("signal_generator import", FAIL, str(exc), "importable")
        return

    # Model .name attributes are lowercase (e.g. TrendModel().name == "trend")
    expected_active   = {"trend", "momentum_breakout", "funding_rate", "sentiment"}
    expected_inactive = {"mean_reversion", "vwap_reversion", "liquidity_sweep", "order_book"}

    # Unwanted models present
    bad_active = active_names & expected_inactive
    if bad_active:
        check("disabled models not in _ALL_MODELS", FAIL,
              f"found: {sorted(bad_active)}", "none of the archived models",
              "Archived models must not be instantiated at runtime")
    else:
        check("disabled models not in _ALL_MODELS", PASS,
              "clean — no archived models in _ALL_MODELS", "none of the archived models")

    # Required models present
    missing_active = expected_active - active_names
    if missing_active:
        check("required models in _ALL_MODELS", FAIL,
              f"missing: {sorted(missing_active)}", sorted(expected_active))
    else:
        check("required models in _ALL_MODELS", PASS,
              sorted(active_names), sorted(expected_active))


# ─────────────────────────────────────────────────────────────
# 4. SCANNER — MTF tf_map
# ─────────────────────────────────────────────────────────────
def validate_scanner() -> None:
    print(f"\n{BOLD}[4] Scanner — MTF tf_map{RESET}")
    try:
        import inspect
        from core.scanning import scanner as _scanner_mod
        src = inspect.getsource(_scanner_mod)
    except Exception as exc:
        check("scanner.py importable", FAIL, str(exc), "importable")
        return

    # Check 30m → 4h mapping (not 1h)
    if '"30m": "4h"' in src:
        check("tf_map[30m]", PASS, "4h", "4h",
              "Phase 5 winning: 30m primary → 4h HTF gate")
    elif '"30m": "1h"' in src:
        check("tf_map[30m]", FAIL, "1h", "4h",
              "Phase 5 requires 4h HTF gate — v1.1 had 1h (wrong)")
    else:
        check("tf_map[30m]", WARN, "not found in source", "4h",
              "Could not verify 30m mapping in scanner source")

    # Check default timeframe in singleton
    if 'AssetScanner(timeframe="30m")' in src or "AssetScanner(timeframe='30m')" in src:
        check("scanner singleton default TF", PASS, "30m", "30m")
    else:
        check("scanner singleton default TF", WARN, "not confirmed", "30m",
              "Verify scanner = AssetScanner(timeframe='30m') in scanner.py")


# ─────────────────────────────────────────────────────────────
# 5. PAPER EXECUTOR — partial close mechanics
# ─────────────────────────────────────────────────────────────
def validate_paper_executor() -> None:
    print(f"\n{BOLD}[5] Paper Executor — Partial Close Mechanics{RESET}")
    try:
        import inspect
        from core.execution import paper_executor as _pe_mod
        src = inspect.getsource(_pe_mod)
    except Exception as exc:
        check("paper_executor.py importable", FAIL, str(exc), "importable")
        return

    # _auto_partial_applied flag
    if "_auto_partial_applied" in src:
        check("_auto_partial_applied flag present", PASS,
              "flag defined in PaperPosition", "present")
    else:
        check("_auto_partial_applied flag present", FAIL,
              "not found", "present",
              "v1.2 requires flag to prevent double-trigger across restarts")

    # Flag persisted to dict (restart safety)
    if '"_auto_partial_applied"' in src or "'_auto_partial_applied'" in src:
        check("_auto_partial_applied serialised to_dict()", PASS,
              "serialised", "serialised")
    else:
        check("_auto_partial_applied serialised to_dict()", FAIL,
              "not found in to_dict", "serialised",
              "Flag must be persisted so restarts cannot re-trigger partial exit")

    # exit.mode config gate
    if "exit.mode" in src:
        check("exit.mode config gate in on_tick()", PASS, "present", "present",
              "Partial exit reads exit.mode from config at runtime")
    else:
        check("exit.mode config gate in on_tick()", FAIL, "not found", "present",
              "Auto-partial logic must read exit.mode from settings")

    # partial_close called on trigger
    if "self.partial_close" in src:
        check("partial_close() called on trigger", PASS, "present", "present")
    else:
        check("partial_close() called on trigger", FAIL, "not found", "present")


# ─────────────────────────────────────────────────────────────
# 6. NOTIFICATIONS — partial_exit template
# ─────────────────────────────────────────────────────────────
def validate_notifications() -> None:
    print(f"\n{BOLD}[6] Notifications — Partial Exit Template{RESET}")
    try:
        from core.notifications.notification_templates import TEMPLATES
        _tmpl_ok = True
    except Exception as exc:
        check("notification_templates importable", FAIL, str(exc), "importable")
        return

    # partial_exit in TEMPLATES registry
    if "partial_exit" in TEMPLATES:
        check("partial_exit in TEMPLATES registry", PASS, "registered", "registered")
    else:
        check("partial_exit in TEMPLATES registry", FAIL, "not found", "registered",
              "v1.2 requires partial_exit template (HTML dark-theme)")

    # partial_exit routing in notification_manager
    try:
        import inspect
        from core.notifications import notification_manager as _nm_mod
        src = inspect.getsource(_nm_mod)
        if "partial_exit" in src and "_on_partial_exit" in src:
            check("partial_exit routing in notification_manager", PASS,
                  "_on_partial_exit handler present", "present")
        else:
            check("partial_exit routing in notification_manager", FAIL,
                  "handler not found", "_on_partial_exit present")
    except Exception as exc:
        check("notification_manager importable", WARN, str(exc), "importable")


# ─────────────────────────────────────────────────────────────
# 7. BACKTEST PARITY ESTIMATES
# ─────────────────────────────────────────────────────────────
def validate_parity_estimates() -> None:
    """
    Check live demo stats against Phase 5 backtest baselines.
    Requires ≥20 live trades to be meaningful; advisory-only below that.
    """
    print(f"\n{BOLD}[7] Live vs Backtest Parity (advisory — requires ≥20 trades){RESET}")
    try:
        from core.database.engine import get_engine
        from core.database.models import Trade
        from sqlalchemy.orm import Session as DBSession
        import sqlalchemy as sa

        engine = get_engine()
        with DBSession(engine) as session:
            rows = session.execute(
                sa.text("SELECT exit_reason, pnl_usdt, side FROM trades "
                        "WHERE status='closed' ORDER BY closed_at DESC LIMIT 200")
            ).fetchall()

        n = len(rows)
        if n < 5:
            check("trade count for parity check", WARN, n, "≥20",
                  f"Only {n} closed trades — parity analysis not yet meaningful")
            return

        wins = sum(1 for r in rows if (r[1] or 0) > 0)
        wr   = wins / n * 100
        gross_win  = sum(r[1] for r in rows if (r[1] or 0) > 0)
        gross_loss = abs(sum(r[1] for r in rows if (r[1] or 0) < 0))
        pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")

        # WR check (Phase 5 combined WR ~57%)
        wr_status = PASS if wr >= 50 else (WARN if wr >= 45 else FAIL)
        check("live WR vs backtest baseline", wr_status, f"{wr:.1f}%", "≥50% (backtest ~57%)",
              f"n={n}; advisory if <20 trades")

        # PF check (Phase 5 combined PF ~2.976)
        pf_status = PASS if pf >= 1.5 else (WARN if pf >= 1.1 else FAIL)
        check("live PF vs backtest baseline", pf_status, f"{pf:.3f}", "≥1.5 (backtest ~2.976)",
              f"n={n}; advisory if <50 trades")

        # Partial exit usage
        partial_count = sum(1 for r in rows if (r[0] or "").startswith("partial"))
        pct_partial = partial_count / n * 100 if n > 0 else 0
        p_status = PASS if pct_partial > 5 else WARN
        check("partial exit events recorded", p_status,
              f"{partial_count}/{n} ({pct_partial:.1f}%)", ">5% of closes should be partial",
              "Confirms auto-partial-close is triggering")

    except Exception as exc:
        check("live trade DB access", WARN, str(exc), "accessible",
              "DB may be empty or not yet initialised")


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(
        description="NexusTrader v1.2 parity validation")
    parser.add_argument("--strict",      action="store_true",
                        help="Treat WARN as FAIL")
    parser.add_argument("--json-output", action="store_true",
                        help="Print JSON results to stdout instead of table")
    args = parser.parse_args()

    print(f"\n{BOLD}{'='*60}")
    print(f"  NexusTrader v1.2 — Parity Validation")
    print(f"  Phase 5 Config: 30m TF, 4h MTF, ADX 31, conf 0.45")
    print(f"  Exit: partial 33% at 1R + breakeven SL")
    print(f"{'='*60}{RESET}")

    validate_version()
    validate_config()
    validate_signal_generator()
    validate_scanner()
    validate_paper_executor()
    validate_notifications()
    validate_parity_estimates()

    # ── Summary ───────────────────────────────────────────────
    if args.json_output:
        print(json.dumps(_results, indent=2))
        fail_count = sum(1 for r in _results if r["status"] == FAIL)
        return 1 if fail_count else 0

    print(f"\n{BOLD}{'─'*60}")
    print(f"  RESULTS SUMMARY")
    print(f"{'─'*60}{RESET}")

    max_name = max(len(r["name"]) for r in _results) if _results else 20
    for r in _results:
        color  = STATUS_COLOR[r["status"]]
        badge  = f"[{r['status']}]"
        name   = r["name"].ljust(max_name)
        actual = r["actual"]
        note   = f"  ({r['note']})" if r["note"] else ""
        print(f"  {color}{badge:6}{RESET} {name}  actual={actual}{note}")

    total    = len(_results)
    n_pass   = sum(1 for r in _results if r["status"] == PASS)
    n_warn   = sum(1 for r in _results if r["status"] == WARN)
    n_fail   = sum(1 for r in _results if r["status"] == FAIL)

    effective_fail = n_fail + (n_warn if args.strict else 0)

    print(f"\n{BOLD}  Total: {total}  |  "
          f"{GREEN}PASS: {n_pass}{RESET}{BOLD}  |  "
          f"{YELLOW}WARN: {n_warn}{RESET}{BOLD}  |  "
          f"{RED}FAIL: {n_fail}{RESET}{BOLD}")

    if effective_fail == 0:
        print(f"\n  {GREEN}✅  RELEASE DECISION: READY{RESET}")
        print(f"  All checks passed. v1.2 configuration verified against")
        print(f"  Phase 5 backtest baselines. Safe to proceed with demo session.")
        rc = 0
    elif n_fail == 0 and n_warn > 0 and not args.strict:
        print(f"\n  {YELLOW}⚠️   RELEASE DECISION: PROCEED WITH CAUTION{RESET}")
        print(f"  No failures, but {n_warn} warning(s) require review before")
        print(f"  scaling to live trading.")
        rc = 2
    else:
        print(f"\n  {RED}❌  RELEASE DECISION: NOT READY{RESET}")
        print(f"  {effective_fail} check(s) failed. Resolve all FAIL items before")
        print(f"  proceeding with demo session.")
        rc = 1

    print(f"{BOLD}{'='*60}{RESET}\n")
    return rc


if __name__ == "__main__":
    sys.exit(main())
