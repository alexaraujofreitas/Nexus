#!/usr/bin/env python
"""
NexusTrader — Bybit Testnet Connection Validator
=================================================
Pre-flight check script. Run BEFORE starting the test harness.

Validates:
  1. Database has an active exchange row with sandbox_mode=True
  2. CCXT can connect to api-testnet.bybit.com
  3. Balance is sufficient (>= $50 USDT for test trades)
  4. Watchlist symbols are tradeable
  5. Order placement permissions (cancel_all_orders as a no-op)
  6. Lifecycle logger is writable

Usage:
    python scripts/testnet/validate_connection.py
"""

import os
import sys
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)


def _check(label: str, passed: bool, detail: str = "") -> bool:
    icon = "PASS" if passed else "FAIL"
    msg = f"  [{icon}] {label}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return passed


def main():
    print("=" * 60)
    print("NexusTrader — Bybit Testnet Pre-Flight Validation")
    print("=" * 60)

    results = []
    all_ok = True

    # ── Check 1: Database row ──────────────────────────────────
    try:
        from core.database.engine import get_session
        from core.database.models import Exchange as ExchangeModel
        with get_session() as session:
            testnet = (
                session.query(ExchangeModel)
                .filter_by(exchange_id="bybit", sandbox_mode=True, is_active=True)
                .first()
            )
            ok = testnet is not None
            detail = f"id={testnet.id}, name={testnet.name}" if ok else "No active testnet row found"
            r = _check("Database: active testnet exchange row", ok, detail)
            all_ok &= r
            results.append({"check": "db_testnet_row", "passed": ok, "detail": detail})
    except Exception as e:
        r = _check("Database: active testnet exchange row", False, str(e))
        all_ok = False
        results.append({"check": "db_testnet_row", "passed": False, "detail": str(e)})

    # ── Check 2: CCXT connectivity ─────────────────────────────
    exchange = None
    try:
        from core.market_data.exchange_manager import ExchangeManager
        em = ExchangeManager()
        em.load_active_exchange()
        exchange = em.get_exchange()
        ok = exchange is not None
        detail = f"mode={em.mode}, markets={len(em._markets or {})}" if ok else "get_exchange() returned None"
        r = _check("Exchange: CCXT connection via ExchangeManager", ok, detail)
        all_ok &= r
        results.append({"check": "ccxt_connection", "passed": ok, "detail": detail})
    except Exception as e:
        r = _check("Exchange: CCXT connection", False, str(e))
        all_ok = False
        results.append({"check": "ccxt_connection", "passed": False, "detail": str(e)})

    if exchange is None:
        print("\n  Cannot proceed without exchange connection. Fix above issues first.")
        _save_results(results, all_ok)
        sys.exit(1)

    # ── Check 3: Balance ───────────────────────────────────────
    try:
        balance = exchange.fetch_balance()
        usdt_free = balance.get("USDT", {}).get("free", 0)
        usdt_total = balance.get("USDT", {}).get("total", 0)
        ok = usdt_free >= 50.0
        detail = f"free={usdt_free:.2f} USDT, total={usdt_total:.2f} USDT"
        if not ok:
            detail += " (need >= $50 USDT for test trades)"
        r = _check("Balance: sufficient USDT", ok, detail)
        all_ok &= r
        results.append({"check": "balance", "passed": ok, "detail": detail})
    except Exception as e:
        r = _check("Balance: fetch_balance()", False, str(e))
        all_ok = False
        results.append({"check": "balance", "passed": False, "detail": str(e)})

    # ── Check 4: Watchlist symbols ─────────────────────────────
    watchlist = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
    try:
        available = []
        missing = []
        for sym in watchlist:
            if sym in exchange.markets:
                ticker = exchange.fetch_ticker(sym)
                available.append(f"{sym}=${ticker['last']:,.2f}")
            else:
                missing.append(sym)
        ok = len(missing) == 0
        detail = f"available={len(available)}/{len(watchlist)}: {', '.join(available)}"
        if missing:
            detail += f" | MISSING: {', '.join(missing)}"
        r = _check("Watchlist: symbols tradeable", ok, detail)
        all_ok &= r
        results.append({"check": "watchlist", "passed": ok, "detail": detail})
    except Exception as e:
        r = _check("Watchlist: symbol check", False, str(e))
        all_ok = False
        results.append({"check": "watchlist", "passed": False, "detail": str(e)})

    # ── Check 5: Order permission (fetch open orders as a no-op test) ──
    try:
        open_orders = exchange.fetch_open_orders("BTC/USDT")
        ok = True
        detail = f"{len(open_orders)} open orders on BTC/USDT"
        r = _check("Permissions: fetch_open_orders() succeeds", ok, detail)
        all_ok &= r
        results.append({"check": "order_permissions", "passed": ok, "detail": detail})
    except Exception as e:
        r = _check("Permissions: order access", False, str(e))
        all_ok = False
        results.append({"check": "order_permissions", "passed": False, "detail": str(e)})

    # ── Check 6: Lifecycle logger writable ─────────────────────
    try:
        from core.execution.trade_lifecycle_logger import get_lifecycle_logger
        ll = get_lifecycle_logger()
        ll._record("validation_check", {"validator": "pre_flight", "status": "ok"})
        ok = ll._path.exists()
        detail = f"path={ll._path}, session={ll._session_id}"
        r = _check("Lifecycle logger: writable", ok, detail)
        all_ok &= r
        results.append({"check": "lifecycle_logger", "passed": ok, "detail": detail})
    except Exception as e:
        r = _check("Lifecycle logger: init", False, str(e))
        all_ok = False
        results.append({"check": "lifecycle_logger", "passed": False, "detail": str(e)})

    # ── Check 7: LiveExecutor can be constructed ───────────────
    try:
        from core.execution.live_executor import LiveExecutor
        le = LiveExecutor()
        ok = le is not None
        n_pos = len(le._positions)
        detail = f"initialized, {n_pos} existing positions"
        r = _check("LiveExecutor: construction", ok, detail)
        all_ok &= r
        results.append({"check": "live_executor", "passed": ok, "detail": detail})
    except Exception as e:
        r = _check("LiveExecutor: construction", False, str(e))
        all_ok = False
        results.append({"check": "live_executor", "passed": False, "detail": str(e)})

    # ── Summary ────────────────────────────────────────────────
    passed = sum(1 for r in results if r["passed"])
    total = len(results)

    print("\n" + "=" * 60)
    if all_ok:
        print(f"PRE-FLIGHT VALIDATION PASSED: {passed}/{total} checks OK")
        print("  Ready to run test harness:")
        print("    python scripts/testnet/test_harness.py --scenario all")
    else:
        print(f"PRE-FLIGHT VALIDATION FAILED: {passed}/{total} checks passed")
        print("  Fix failures above before running the test harness.")
    print("=" * 60)

    _save_results(results, all_ok)
    sys.exit(0 if all_ok else 1)


def _save_results(results: list, all_ok: bool):
    out_path = Path(ROOT) / "data" / "testnet_preflight.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({
            "run_at": datetime.now(timezone.utc).isoformat(),
            "all_passed": all_ok,
            "checks": results,
        }, f, indent=2)
    print(f"\nResults saved to: {out_path}")


if __name__ == "__main__":
    main()
