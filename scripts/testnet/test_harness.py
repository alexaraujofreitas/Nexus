#!/usr/bin/env python
"""
NexusTrader — Testnet Edge-Case Test Harness
==============================================
Injects controlled test scenarios to validate the LiveExecutor
under edge conditions that may not occur naturally during the
50-trade autonomous run.

Scenarios:
  1. Normal buy + SL close (validates full lifecycle)
  2. Normal sell + TP close
  3. Partial close (33%) + residual close (dust check)
  4. Rapid open/close (latency stress)
  5. Simultaneous multi-symbol opens
  6. SL/TP geometry rejection (invalid candidate)
  7. Double-close attempt (rejected by state machine)
  8. Pre-trade gate: exchange disconnected simulation

Usage:
    python scripts/testnet/test_harness.py --scenario all
    python scripts/testnet/test_harness.py --scenario 1
    python scripts/testnet/test_harness.py --scenario 3,5,7

Requires: NexusTrader running with Bybit Testnet connected + Live mode active.
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("test_harness")


def _get_executor():
    """Get the live executor singleton (must already be initialized)."""
    from core.execution.live_executor import LiveExecutor
    # Access the module-level singleton
    import core.execution.live_executor as _mod
    le = getattr(_mod, "live_executor", None)
    if le is None:
        # Create a fresh instance for standalone testing
        le = LiveExecutor()
    return le


def _get_exchange():
    from core.market_data.exchange_manager import ExchangeManager
    em = ExchangeManager()
    return em.get_exchange()


def _make_candidate(symbol, side, score=0.65, size_usdt=50.0,
                    sl_pct=0.02, tp_pct=0.04):
    """Build a minimal OrderCandidate for test injection."""
    from core.meta_decision.order_candidate import OrderCandidate

    ex = _get_exchange()
    if not ex:
        raise RuntimeError("Exchange not connected")

    ticker = ex.fetch_ticker(symbol)
    price = ticker["last"]

    if side == "buy":
        sl = price * (1 - sl_pct)
        tp = price * (1 + tp_pct)
    else:
        sl = price * (1 + sl_pct)
        tp = price * (1 - tp_pct)

    return OrderCandidate(
        symbol=symbol,
        side=side,
        score=score,
        position_size_usdt=size_usdt,
        stop_loss_price=sl,
        take_profit_price=tp,
        entry_price=price,
        timeframe="30m",
        regime="bull_trend" if side == "buy" else "bear_trend",
        models_fired=["test_harness"],
        rationale=f"Test harness {side} @ {price:.2f}",
    )


class TestHarness:
    """Orchestrates edge-case test scenarios on Bybit testnet."""

    def __init__(self):
        self.results = []
        self._start_time = time.time()

    def _record(self, scenario_id: int, name: str, passed: bool, detail: str = ""):
        result = {
            "scenario": scenario_id,
            "name": name,
            "passed": passed,
            "detail": detail,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        self.results.append(result)
        icon = "PASS" if passed else "FAIL"
        logger.info("[%s] Scenario %d: %s — %s", icon, scenario_id, name, detail)

    # ── Scenario 1: Normal Buy + Close ────────────────────────
    def scenario_1(self):
        """Buy position → wait 5s → manual close."""
        name = "Normal buy + manual close"
        try:
            le = _get_executor()
            candidate = _make_candidate("BTC/USDT", "buy", size_usdt=30.0)
            result = le.submit(candidate)
            if not result:
                self._record(1, name, False, "submit() returned False")
                return

            time.sleep(3)
            # Verify position exists
            pos = le._positions.get("BTC/USDT")
            if not pos:
                self._record(1, name, False, "Position not found after submit")
                return

            # Close it
            closed = le.close_position("BTC/USDT")
            time.sleep(2)  # wait for exchange
            self._record(1, name, closed, f"entry={pos.entry_price:.2f}")
        except Exception as e:
            self._record(1, name, False, str(e))

    # ── Scenario 2: Normal Sell + Close ───────────────────────
    def scenario_2(self):
        """Sell (short) position → wait 5s → manual close."""
        name = "Normal sell + manual close"
        try:
            le = _get_executor()
            candidate = _make_candidate("ETH/USDT", "sell", size_usdt=30.0)
            result = le.submit(candidate)
            if not result:
                self._record(2, name, False, "submit() returned False")
                return

            time.sleep(3)
            closed = le.close_position("ETH/USDT")
            time.sleep(2)
            self._record(2, name, closed, "Short position lifecycle complete")
        except Exception as e:
            self._record(2, name, False, str(e))

    # ── Scenario 3: Partial Close + Dust ──────────────────────
    def scenario_3(self):
        """Open → partial close 50% → partial close 99% (dust → full close)."""
        name = "Partial close + dust threshold"
        try:
            le = _get_executor()
            candidate = _make_candidate("BTC/USDT", "buy", size_usdt=50.0)
            result = le.submit(candidate)
            if not result:
                self._record(3, name, False, "submit() returned False")
                return

            time.sleep(3)
            # Partial 1: 50%
            p1 = le.partial_close("BTC/USDT", 0.50)
            time.sleep(2)

            # Check remaining
            pos = le._positions.get("BTC/USDT")
            remaining = pos.quantity if pos else 0

            # Partial 2: 99.5% of remaining → should trigger dust → full close
            p2 = le.partial_close("BTC/USDT", 0.995)
            time.sleep(2)

            still_open = "BTC/USDT" in le._positions
            self._record(3, name, not still_open,
                         f"partial1={p1}, partial2={p2}, remaining={remaining:.8f}, still_open={still_open}")
        except Exception as e:
            self._record(3, name, False, str(e))

    # ── Scenario 4: Rapid Open/Close (Latency Test) ──────────
    def scenario_4(self):
        """Open + close as fast as possible to measure latency."""
        name = "Rapid open/close latency test"
        try:
            le = _get_executor()
            t0 = time.time()
            candidate = _make_candidate("BTC/USDT", "buy", size_usdt=25.0)
            result = le.submit(candidate)
            t1 = time.time()

            if not result:
                self._record(4, name, False, "submit() returned False")
                return

            time.sleep(1)  # minimal wait
            t2 = time.time()
            closed = le.close_position("BTC/USDT")
            t3 = time.time()

            open_ms = (t1 - t0) * 1000
            close_ms = (t3 - t2) * 1000
            total_ms = (t3 - t0) * 1000

            self._record(4, name, closed,
                         f"open={open_ms:.0f}ms close={close_ms:.0f}ms total={total_ms:.0f}ms")
        except Exception as e:
            self._record(4, name, False, str(e))

    # ── Scenario 5: Multi-Symbol Simultaneous Open ────────────
    def scenario_5(self):
        """Open 3 positions simultaneously."""
        name = "Multi-symbol simultaneous open"
        try:
            le = _get_executor()
            symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
            opened = []
            for sym in symbols:
                try:
                    candidate = _make_candidate(sym, "buy", size_usdt=25.0)
                    r = le.submit(candidate)
                    opened.append((sym, r))
                except Exception as exc:
                    opened.append((sym, f"error: {exc}"))

            time.sleep(3)
            n_open = sum(1 for sym, _ in opened if sym in le._positions)

            # Clean up
            for sym, _ in opened:
                try:
                    le.close_position(sym)
                except Exception:
                    pass
            time.sleep(3)

            self._record(5, name, n_open >= 2,
                         f"opened={n_open}/3 results={opened}")
        except Exception as e:
            self._record(5, name, False, str(e))

    # ── Scenario 6: SL/TP Geometry Rejection ──────────────────
    def scenario_6(self):
        """Submit candidate with SL > entry (should be rejected)."""
        name = "SL/TP geometry rejection"
        try:
            from core.meta_decision.order_candidate import OrderCandidate
            ex = _get_exchange()
            ticker = ex.fetch_ticker("BTC/USDT")
            price = ticker["last"]

            # Invalid: SL above entry for a long
            bad = OrderCandidate(
                symbol="BTC/USDT",
                side="buy",
                score=0.70,
                position_size_usdt=30.0,
                stop_loss_price=price * 1.05,   # SL ABOVE entry
                take_profit_price=price * 1.10,
                entry_price=price,
                timeframe="30m",
                regime="bull_trend",
                models_fired=["test_harness"],
                rationale="Invalid geometry test",
            )

            from core.execution.order_router import OrderRouter
            router = OrderRouter()
            rejection = router._validate_candidate(bad)

            self._record(6, name, rejection is not None,
                         f"rejection='{rejection}'")
        except Exception as e:
            self._record(6, name, False, str(e))

    # ── Scenario 7: Double-Close Rejection ────────────────────
    def scenario_7(self):
        """Open position → close → try to close again (should fail gracefully)."""
        name = "Double-close rejection"
        try:
            le = _get_executor()
            candidate = _make_candidate("BTC/USDT", "buy", size_usdt=25.0)
            result = le.submit(candidate)
            if not result:
                self._record(7, name, False, "submit() returned False")
                return

            time.sleep(3)
            close1 = le.close_position("BTC/USDT")
            time.sleep(2)
            close2 = le.close_position("BTC/USDT")  # should return False

            self._record(7, name, close1 and not close2,
                         f"close1={close1}, close2={close2}")
        except Exception as e:
            self._record(7, name, False, str(e))

    # ── Scenario 8: Pre-Trade Gate (Disconnected) ─────────────
    def scenario_8(self):
        """Temporarily set _exchange_connected=False and verify submit blocks."""
        name = "Pre-trade gate: exchange disconnected"
        try:
            le = _get_executor()
            original = le._exchange_connected

            le._exchange_connected = False
            candidate = _make_candidate("BTC/USDT", "buy", size_usdt=25.0)
            result = le.submit(candidate)
            le._exchange_connected = original  # restore immediately

            self._record(8, name, not result,
                         f"submit_while_disconnected={result}")
        except Exception as e:
            self._record(8, name, False, str(e))

    # ── Run scenarios ─────────────────────────────────────────
    def run(self, scenario_ids: list[int]):
        scenario_map = {
            1: self.scenario_1,
            2: self.scenario_2,
            3: self.scenario_3,
            4: self.scenario_4,
            5: self.scenario_5,
            6: self.scenario_6,
            7: self.scenario_7,
            8: self.scenario_8,
        }

        for sid in scenario_ids:
            fn = scenario_map.get(sid)
            if fn:
                logger.info("=" * 60)
                logger.info("Running Scenario %d: %s", sid, fn.__doc__.strip())
                logger.info("=" * 60)
                fn()
                time.sleep(2)  # cooldown between scenarios
            else:
                logger.warning("Unknown scenario: %d", sid)

        # Summary
        elapsed = time.time() - self._start_time
        passed = sum(1 for r in self.results if r["passed"])
        failed = sum(1 for r in self.results if not r["passed"])
        total = len(self.results)

        print("\n" + "=" * 60)
        print(f"TEST HARNESS RESULTS: {passed}/{total} passed, {failed} failed ({elapsed:.1f}s)")
        print("=" * 60)
        for r in self.results:
            icon = "PASS" if r["passed"] else "FAIL"
            print(f"  [{icon}] Scenario {r['scenario']}: {r['name']}")
            if r["detail"]:
                print(f"         {r['detail']}")

        # Save results
        out_path = os.path.join(ROOT, "data", "test_harness_results.json")
        with open(out_path, "w") as f:
            json.dump({
                "run_at": datetime.now(timezone.utc).isoformat(),
                "elapsed_s": round(elapsed, 1),
                "passed": passed,
                "failed": failed,
                "total": total,
                "results": self.results,
            }, f, indent=2)
        print(f"\nResults saved to: {out_path}")


def main():
    parser = argparse.ArgumentParser(description="NexusTrader Testnet Edge-Case Test Harness")
    parser.add_argument("--scenario", default="all",
                        help="Scenario IDs to run: 'all' or comma-separated (e.g. '1,3,5')")
    args = parser.parse_args()

    if args.scenario == "all":
        scenario_ids = list(range(1, 9))
    else:
        scenario_ids = [int(s.strip()) for s in args.scenario.split(",")]

    harness = TestHarness()
    harness.run(scenario_ids)


if __name__ == "__main__":
    main()
