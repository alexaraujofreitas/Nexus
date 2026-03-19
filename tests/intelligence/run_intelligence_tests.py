#!/usr/bin/env python3
"""
NexusTrader — Intelligence Agent Test Runner
=============================================

Executes the comprehensive intelligence agent test suite, captures
detailed results, and prints a formatted report to stdout.

Usage:
    python tests/intelligence/run_intelligence_tests.py
    python tests/intelligence/run_intelligence_tests.py --include-slow
    python tests/intelligence/run_intelligence_tests.py --verbose
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime


def main():
    parser = argparse.ArgumentParser(
        description="Run NexusTrader Intelligence Agent tests"
    )
    parser.add_argument(
        "--include-slow", action="store_true",
        help="Include @pytest.mark.slow performance tests (takes longer)"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Show individual test names as they run"
    )
    parser.add_argument(
        "--class", dest="test_class",
        help="Run only a specific test class (e.g. TestFundingRateAgent)"
    )
    args = parser.parse_args()

    # Build pytest command
    test_path = os.path.join(
        os.path.dirname(__file__),
        "test_intelligence_agents.py"
    )

    cmd = [sys.executable, "-m", "pytest", test_path, "--tb=short", "--no-header"]

    if args.verbose:
        cmd.append("-v")
    else:
        cmd.append("-q")

    if not args.include_slow:
        cmd.extend(["-m", "not slow"])

    if args.test_class:
        cmd.append(f"tests/intelligence/test_intelligence_agents.py::{args.test_class}")

    print("=" * 70)
    print("NexusTrader Intelligence Agent Test Suite")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)
    print(f"Command: {' '.join(cmd)}")
    print()

    start_time = time.monotonic()

    try:
        result = subprocess.run(cmd, capture_output=False, text=True)
        elapsed = time.monotonic() - start_time

        print()
        print("=" * 70)
        print(f"Completed in {elapsed:.2f}s")
        print(f"Exit code: {result.returncode}")
        if result.returncode == 0:
            print("✅ ALL TESTS PASSED")
        else:
            print("❌ SOME TESTS FAILED — review output above")
        print("=" * 70)
        sys.exit(result.returncode)

    except KeyboardInterrupt:
        print("\nAborted by user.")
        sys.exit(1)
    except FileNotFoundError:
        print(f"ERROR: pytest not found. Run: pip install pytest")
        sys.exit(1)


if __name__ == "__main__":
    main()
