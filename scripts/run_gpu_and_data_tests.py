#!/usr/bin/env python
"""
run_gpu_and_data_tests.py
=========================
Desktop runner for tests that require PyTorch (GPU) or backtest data files.
These tests cannot run in the VM sandbox due to:
  - PyTorch: 530MB+ install, VM has insufficient disk space
  - Backtest data: parquet files not present in VM

Run from the NexusTrader project root on your Windows desktop:

    python scripts/run_gpu_and_data_tests.py

Prerequisites:
    - PyTorch (CUDA cu124) installed:
        pip install "torch>=2.6.0" --index-url https://download.pytorch.org/whl/cu124
    - gymnasium installed:
        pip install gymnasium
    - backtest_data/ directory with BTC_USDT_30m.parquet (for backtest parity tests)
    - Set NEXUS_RUN_BACKTEST=1 to enable backtest parity tests

The script runs all tests and prints a summary.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

# ── Test groups ──────────────────────────────────────────────────────

# Group 1: PyTorch / RL Ensemble tests (8 tests — require torch + gymnasium)
PYTORCH_TESTS = [
    "tests/unit/test_log_review_fixes.py::TestRLEnsembleRegimeNames::test_lr03_sac_includes_bull_trend",
    "tests/unit/test_log_review_fixes.py::TestRLEnsembleRegimeNames::test_lr03_sac_includes_bear_trend",
    "tests/unit/test_log_review_fixes.py::TestRLEnsembleRegimeNames::test_lr03_sac_includes_uncertain",
    "tests/unit/test_log_review_fixes.py::TestRLEnsembleRegimeNames::test_lr03_sac_includes_volatility_expansion",
    "tests/unit/test_log_review_fixes.py::TestRLEnsembleRegimeNames::test_lr03_cppo_includes_bear_trend",
    "tests/unit/test_log_review_fixes.py::TestRLEnsembleRegimeNames::test_lr03_cppo_includes_volatility_expansion",
    "tests/unit/test_log_review_fixes.py::TestRLEnsembleRegimeNames::test_lr03_legacy_names_preserved",
    "tests/unit/test_log_review_fixes.py::TestRLEnsembleRegimeNames::test_lr03_select_action_uncertain_no_warning",
]

# Group 2: Backtest parity tests (6 tests — require backtest_data/ + NEXUS_RUN_BACKTEST=1)
BACKTEST_TESTS = [
    "tests/unit/test_session40_unified_engine.py::TestParityPblSlc::test_pbl_slc_n_trades",
    "tests/unit/test_session40_unified_engine.py::TestParityPblSlc::test_pbl_slc_pf_zero_fees",
    "tests/unit/test_session40_unified_engine.py::TestParityPblSlc::test_pbl_slc_pf_with_fees",
    "tests/unit/test_session40_unified_engine.py::TestParityPblSlc::test_unified_route_pbl_slc_calls_reference",
    "tests/unit/test_session40_unified_engine.py::TestParityPblSlc::test_trend_mode_produces_trades",
    "tests/unit/test_session40_unified_engine.py::TestParityPblSlc::test_full_system_mode_runs",
]


def _check_pytorch() -> bool:
    try:
        import torch
        print(f"  PyTorch {torch.__version__}  CUDA: {torch.cuda.is_available()}")
        if torch.cuda.is_available():
            print(f"  GPU: {torch.cuda.get_device_name(0)}")
        return True
    except ImportError:
        return False


def _check_backtest_data() -> bool:
    data_file = ROOT / "backtest_data" / "BTC_USDT_30m.parquet"
    exists = data_file.exists()
    env_set = os.getenv("NEXUS_RUN_BACKTEST", "0") == "1"
    print(f"  backtest_data/BTC_USDT_30m.parquet: {'found' if exists else 'MISSING'}")
    print(f"  NEXUS_RUN_BACKTEST env var: {'1 (set)' if env_set else '0 (NOT set)'}")
    return exists and env_set


def _run_tests(test_ids: list[str], label: str) -> tuple[int, int, int]:
    """Run test group via pytest subprocess. Returns (passed, failed, skipped)."""
    cmd = [
        sys.executable, "-m", "pytest",
        *test_ids,
        "-v", "--tb=short",
    ]
    print(f"\n{'=' * 60}")
    print(f"  Running: {label} ({len(test_ids)} tests)")
    print(f"{'=' * 60}\n")

    result = subprocess.run(cmd, cwd=str(ROOT))

    # Parse exit code: 0=all passed, 1=some failed, 5=no tests collected
    if result.returncode == 0:
        return (len(test_ids), 0, 0)
    elif result.returncode == 5:
        return (0, 0, len(test_ids))
    else:
        # Can't parse exact counts from exit code alone — user reads output
        return (0, len(test_ids), 0)


def main():
    print("=" * 60)
    print("  NexusTrader — Desktop Test Runner (GPU + Data)")
    print("=" * 60)

    # ── Check prerequisites ──
    print("\n[1/2] Checking PyTorch...")
    has_pytorch = _check_pytorch()

    print("\n[2/2] Checking backtest data...")
    has_data = _check_backtest_data()

    total_passed = 0
    total_failed = 0
    total_skipped = 0

    # ── Run PyTorch tests ──
    if has_pytorch:
        p, f, s = _run_tests(PYTORCH_TESTS, "PyTorch / RL Ensemble Tests")
        total_passed += p
        total_failed += f
        total_skipped += s
    else:
        print("\n⚠  PyTorch not installed — skipping 8 RL Ensemble tests.")
        print("   Install with: pip install 'torch>=2.6.0' --index-url https://download.pytorch.org/whl/cu124")
        total_skipped += len(PYTORCH_TESTS)

    # ── Run backtest data tests ──
    if has_data:
        p, f, s = _run_tests(BACKTEST_TESTS, "Backtest Parity Tests")
        total_passed += p
        total_failed += f
        total_skipped += s
    else:
        print("\n⚠  Backtest data not available — skipping 6 parity tests.")
        if not (ROOT / "backtest_data" / "BTC_USDT_30m.parquet").exists():
            print("   Place backtest_data/BTC_USDT_30m.parquet in project root.")
        if os.getenv("NEXUS_RUN_BACKTEST", "0") != "1":
            print("   Set environment variable: NEXUS_RUN_BACKTEST=1")
        total_skipped += len(BACKTEST_TESTS)

    # ── Summary ──
    print("\n" + "=" * 60)
    print("  SUMMARY")
    print("=" * 60)
    print(f"  Passed:  {total_passed}")
    print(f"  Failed:  {total_failed}")
    print(f"  Skipped: {total_skipped}")
    print(f"  Total:   {total_passed + total_failed + total_skipped}")
    print("=" * 60)

    return 1 if total_failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
