"""
tests/unit/test_scanner_performance_benchmark.py
-------------------------------------------------
Performance benchmark for the v2 parallel pipeline scanner.

Simulates a full 20-symbol scan cycle with mock exchange to measure
wall-clock time without network I/O variability.

Target: 20 symbols ≤ 10 seconds end-to-end with ~50ms simulated fetch latency.

Note: HMM fitting and MS-GARCH fitting are one-time first-scan costs that are
mocked here to measure steady-state pipeline performance (which is the common
case — after the first scan, HMMs and GARCH are cached).
"""
from __future__ import annotations

import time
import numpy as np
import warnings
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

# Suppress noisy warnings from numerical libraries
warnings.filterwarnings("ignore")

# Disable background agents before they auto-start during imports.
# Agents use exponential-backoff sleeps that would hang the benchmark.
try:
    import core.agents.base_agent as _ba
    _ba.BaseAgent.run = lambda self: None
    _ba.BaseAgent.start = lambda self: None
except ImportError:
    pass


# 20 configured symbols matching config.yaml
BENCHMARK_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT", "SOL/USDT",
    "TRX/USDT", "DOGE/USDT", "ADA/USDT", "BCH/USDT", "HYPE/USDT",
    "LINK/USDT", "XLM/USDT", "AVAX/USDT", "HBAR/USDT", "SUI/USDT",
    "NEAR/USDT", "ICP/USDT", "ONDO/USDT", "ALGO/USDT", "RENDER/USDT",
]


def _make_mock_ohlcv(n_bars: int = 300, base_price: float = 50000.0) -> list:
    """Generate realistic mock OHLCV data."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    interval_ms = 3600_000  # 1h candles
    data = []
    price = float(base_price)
    for i in range(n_bars):
        ts = now_ms - (n_bars - i) * interval_ms
        change = np.random.normal(0, 0.005)
        price *= (1 + change)
        o = price
        h = price * (1 + abs(np.random.normal(0, 0.003)))
        l = price * (1 - abs(np.random.normal(0, 0.003)))
        c = price * (1 + np.random.normal(0, 0.002))
        v = abs(np.random.normal(1000, 300))
        data.append([ts, o, h, l, c, v])
    return data


class MockExchange:
    """Mock exchange that simulates network latency (50ms per call)."""

    def __init__(self, latency_ms: float = 50):
        self._latency_s = latency_ms / 1000
        self._ohlcv_cache = {}

    def fetch_tickers(self, symbols):
        time.sleep(self._latency_s)
        return {
            s: {"bid": 100 * 0.999, "ask": 100 * 1.001,
                "baseVolume": 10000, "quoteVolume": 1_000_000}
            for s in symbols
        }

    def fetch_ohlcv(self, symbol, timeframe, limit=300):
        time.sleep(self._latency_s)
        key = f"{symbol}:{timeframe}:{limit}"
        if key not in self._ohlcv_cache:
            base = 50000 if "BTC" in symbol else 3000 if "ETH" in symbol else 100
            self._ohlcv_cache[key] = _make_mock_ohlcv(limit, base)
        return self._ohlcv_cache[key]


def _make_worker(symbols=None, exchange=None):
    """Create a ScanWorker with mocked heavy components."""
    from core.scanning.scanner import ScanWorker

    symbols = symbols or BENCHMARK_SYMBOLS
    exchange = exchange or MockExchange(latency_ms=50)

    worker = ScanWorker(
        symbols=symbols,
        timeframe="1h",
        exchange=exchange,
        open_positions=[],
        capital_usdt=10000.0,
        drawdown_pct=0.0,
    )
    # Skip HMM fitting (simulates steady-state: HMMs already fitted)
    worker._use_hmm = False
    # Mock universe filter to pass all symbols through
    worker._univ_filter = MagicMock()
    worker._univ_filter.apply.return_value = symbols
    # Mock risk gate
    worker._risk_gate = MagicMock()
    worker._risk_gate.validate_batch.return_value = ([], [])
    # Patch Qt signals to avoid Qt event loop dependency
    for sig in ["scan_complete", "scan_error", "symbol_scanned",
                "df_cache_updated", "scan_all_results"]:
        setattr(worker, sig, MagicMock())

    return worker


# Mock MS-GARCH to avoid expensive fitting
_MOCK_GARCH = MagicMock()
_MOCK_GARCH.forecast.return_value = {"confidence": 0.5, "vol_state": "low"}
_MOCK_GARCH.get_regime_adjustment.side_effect = lambda r, f: r


class TestScannerPerformanceBenchmark:
    """Full cycle benchmark with 20 symbols (steady-state: no first-fit costs)."""

    def test_benchmark_20_symbols_under_10s(self):
        """
        ACCEPTANCE TEST: Full scan cycle for 20 symbols must complete in ≤10 seconds.
        Uses mock exchange with 50ms latency per REST call.
        """
        worker = _make_worker()

        with patch("core.scanning.scanner.bus"), \
             patch("core.regime.ms_garch_forecaster.ms_garch", _MOCK_GARCH):
            t0 = time.time()
            worker.run()
            elapsed_s = time.time() - t0

        elapsed_ms = elapsed_s * 1000
        print(f"\n{'=' * 60}")
        print(f"  BENCHMARK: 20 symbols in {elapsed_ms:.0f}ms ({elapsed_s:.2f}s)")
        print(f"  Target: ≤10,000ms")
        print(f"  Status: {'✓ PASS' if elapsed_s <= 10.0 else '✗ FAIL'}")
        print(f"{'=' * 60}\n")

        worker.scan_complete.emit.assert_called_once()
        worker.scan_error.emit.assert_not_called()
        assert elapsed_s <= 10.0, (
            f"Full scan cycle took {elapsed_s:.1f}s — exceeds 10s target."
        )

    def test_benchmark_all_symbols_produce_results(self):
        """Every symbol must produce a result."""
        worker = _make_worker()

        with patch("core.scanning.scanner.bus"), \
             patch("core.regime.ms_garch_forecaster.ms_garch", _MOCK_GARCH):
            worker.run()

        worker.scan_all_results.emit.assert_called_once()
        all_results = worker.scan_all_results.emit.call_args[0][0]
        result_symbols = {r["symbol"] for r in all_results}
        for sym in BENCHMARK_SYMBOLS:
            assert sym in result_symbols, f"Symbol {sym} missing from scan results"

    def test_benchmark_repeated_cycles_stable(self):
        """Multiple consecutive scan cycles should not degrade in performance."""
        timings = []
        for cycle in range(3):
            exchange = MockExchange(latency_ms=30)
            worker = _make_worker(exchange=exchange)
            with patch("core.scanning.scanner.bus"), \
                 patch("core.regime.ms_garch_forecaster.ms_garch", _MOCK_GARCH):
                t0 = time.time()
                worker.run()
                elapsed = time.time() - t0
                timings.append(elapsed)

        print(f"\n  Cycle timings: {[f'{t:.2f}s' for t in timings]}")

        fastest = min(timings)
        for i, t in enumerate(timings):
            assert t <= fastest * 3.0, (
                f"Cycle {i + 1} took {t:.2f}s vs fastest ({fastest:.2f}s). "
                "Performance may be degrading."
            )

    def test_benchmark_failure_isolation(self):
        """If one symbol fails, others still produce results."""
        class FailingExchange(MockExchange):
            def fetch_ohlcv(self, symbol, timeframe, limit=300):
                if symbol == "XRP/USDT":
                    raise ConnectionError("Simulated failure for XRP")
                return super().fetch_ohlcv(symbol, timeframe, limit)

        worker = _make_worker(exchange=FailingExchange(latency_ms=20))

        with patch("core.scanning.scanner.bus"), \
             patch("core.regime.ms_garch_forecaster.ms_garch", _MOCK_GARCH):
            worker.run()

        worker.scan_complete.emit.assert_called_once()
        all_results = worker.scan_all_results.emit.call_args[0][0]
        # At least 19 out of 20 should have results
        assert len(all_results) >= 19
