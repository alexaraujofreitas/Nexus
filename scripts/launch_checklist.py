#!/usr/bin/env python3
"""
NexusTrader — Pre-Launch Validation Checklist

Safe validation script that checks system readiness before paper trading launch.
Exit codes:
  0 = all checks pass
  1 = critical failure detected
  2 = warnings only (no failures)

Usage: python scripts/launch_checklist.py
"""

import sys
from pathlib import Path
import sqlite3
from typing import Tuple, List

# Inject project root into sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ANSI colour codes
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BOLD = "\033[1m"
RESET = "\033[0m"


class ChecklistRunner:
    """Runs pre-launch validation checks."""

    def __init__(self):
        self.passed = []
        self.warnings = []
        self.failures = []

    def print_section(self, title: str):
        """Print a section header."""
        print(f"\n{BOLD}{title}{RESET}")
        print("─" * 70)

    def pass_check(self, message: str):
        """Record a passing check."""
        self.passed.append(message)
        print(f"{GREEN}✓{RESET} {message}")

    def warn_check(self, message: str):
        """Record a warning (non-blocking)."""
        self.warnings.append(message)
        print(f"{YELLOW}⚠{RESET} {message}")

    def fail_check(self, message: str):
        """Record a failure (blocking)."""
        self.failures.append(message)
        print(f"{RED}✗{RESET} {message}")

    def print_summary(self):
        """Print final summary and return exit code."""
        print(f"\n{BOLD}{'=' * 70}{RESET}")
        total = len(self.passed) + len(self.warnings) + len(self.failures)
        summary = f"RESULT: {len(self.passed)} PASS | {len(self.warnings)} WARN | {len(self.failures)} FAIL | {total} total"
        print(summary)
        print(f"{BOLD}{'=' * 70}{RESET}\n")

        if self.failures:
            return 1
        elif self.warnings:
            return 2
        else:
            return 0

    # =========================================================================
    # SECTION 1: CONFIGURATION
    # =========================================================================

    def check_config(self):
        """Check configuration files."""
        self.print_section("Section 1: Configuration")

        config_path = _PROJECT_ROOT / "config.yaml"

        # Check config.yaml readable and parseable
        if not config_path.exists():
            self.fail_check("config.yaml not found")
            return

        try:
            import yaml

            with open(config_path, "r") as f:
                config = yaml.safe_load(f)
            self.pass_check(f"config.yaml readable and parseable")
        except Exception as e:
            self.fail_check(f"config.yaml parse error: {e}")
            return

        # Check scanner.auto_execute
        scanner_config = config.get("scanner", {})
        auto_execute = scanner_config.get("auto_execute", None)
        if auto_execute is True:
            self.pass_check("scanner.auto_execute == True")
        else:
            self.fail_check(f"scanner.auto_execute is {auto_execute}, must be True")

        # Check rl.enabled present
        rl_config = config.get("rl", {})
        if "enabled" in rl_config:
            self.pass_check(f"rl.enabled present (value: {rl_config['enabled']})")
        else:
            self.fail_check("rl.enabled not found in config")

        # Check risk_engine.risk_pct_per_trade
        risk_config = config.get("risk_engine", {})
        risk_pct = risk_config.get("risk_pct_per_trade", None)
        if risk_pct is not None:
            if risk_pct <= 2.0:
                self.pass_check(
                    f"risk_engine.risk_pct_per_trade present ({risk_pct}%, paper guard OK)"
                )
            else:
                self.warn_check(
                    f"risk_engine.risk_pct_per_trade = {risk_pct}% (high for paper trading)"
                )
        else:
            self.fail_check("risk_engine.risk_pct_per_trade not found")

        # Check disabled_models list present
        if "disabled_models" in config:
            disabled = config["disabled_models"]
            self.pass_check(f"disabled_models list present ({len(disabled)} items)")
        else:
            self.fail_check("disabled_models list not found")

    # =========================================================================
    # SECTION 2: DATABASE
    # =========================================================================

    def check_database(self):
        """Check database existence and writability."""
        self.print_section("Section 2: Database")

        db_path = _PROJECT_ROOT / "data" / "nexus_trader.db"

        # Check DB file exists
        if db_path.exists():
            self.pass_check(f"DB file exists ({db_path})")
        else:
            self.fail_check(f"DB file not found: {db_path}")
            return

        # Check DB writable
        try:
            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()

            # Test write access with a test row in system_logs
            cursor.execute(
                "INSERT INTO system_logs (level, module, message, timestamp) VALUES (?, ?, ?, ?)",
                ("TEST", "launch_checklist", "launch_checklist_test", "2026-03-25T00:00:00Z"),
            )
            test_id = cursor.lastrowid
            conn.commit()

            # Clean up test row
            cursor.execute("DELETE FROM system_logs WHERE id = ?", (test_id,))
            conn.commit()
            conn.close()

            self.pass_check("DB writable (test insert/delete successful)")
        except sqlite3.OperationalError as e:
            if "no such table" in str(e):
                self.fail_check(f"DB table missing: {e}")
            else:
                self.fail_check(f"DB not writable: {e}")
            return
        except Exception as e:
            self.fail_check(f"DB write test failed: {e}")
            return

        # Check required tables exist
        try:
            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()

            required_tables = [
                "paper_trades",
                "trade_feedback",
                "strategy_tuning_proposals",
                "applied_strategy_changes",
            ]

            for table_name in required_tables:
                cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table_name,),
                )
                if cursor.fetchone():
                    self.pass_check(f"Table '{table_name}' exists")
                else:
                    self.fail_check(f"Required table '{table_name}' not found")

            conn.close()
        except Exception as e:
            self.fail_check(f"Table check failed: {e}")

    # =========================================================================
    # SECTION 3: DATA FEEDS
    # =========================================================================

    def check_data_feeds(self):
        """Check data feed files."""
        self.print_section("Section 3: Data Feeds")

        data_validation_path = _PROJECT_ROOT / "data" / "validation"

        # Check data/validation/ directory exists
        if data_validation_path.exists() and data_validation_path.is_dir():
            self.pass_check(f"data/validation/ directory exists")
        else:
            self.fail_check(f"data/validation/ directory not found")
            return

        # Check for at least one *_1h.parquet file
        parquet_files = list(data_validation_path.glob("*_1h.parquet"))
        if parquet_files:
            self.pass_check(
                f"Found {len(parquet_files)} 1h parquet files for backtest runner"
            )
        else:
            self.warn_check("No *_1h.parquet files found in data/validation/")

        # Count all parquet files
        all_parquets = list(data_validation_path.glob("*.parquet"))
        if len(all_parquets) < 3:
            self.warn_check(
                f"Fewer than 3 parquet files in data/validation/ ({len(all_parquets)} found)"
            )
        else:
            self.pass_check(f"Parquet file count adequate ({len(all_parquets)} files)")

        # Check websocket_enabled config (informational only)
        try:
            import yaml

            config_path = _PROJECT_ROOT / "config.yaml"
            with open(config_path, "r") as f:
                config = yaml.safe_load(f)
            ws_enabled = config.get("data", {}).get("websocket_enabled", False)
            if ws_enabled:
                self.warn_check(
                    "websocket_enabled: true (note: WS disabled per production config advice)"
                )
            else:
                self.pass_check("websocket_enabled: false (REST polling, as recommended)")
        except Exception as e:
            self.warn_check(f"Could not check websocket_enabled: {e}")

    # =========================================================================
    # SECTION 4: ANALYSIS PIPELINE
    # =========================================================================

    def check_analysis_pipeline(self):
        """Check trade analysis service and dependencies."""
        self.print_section("Section 4: Analysis Pipeline")

        # Check trade_analysis_service importable
        try:
            from core.analysis.trade_analysis_service import trade_analysis_service
            assert hasattr(trade_analysis_service, "build_open_trade_analysis")
            self.pass_check("trade_analysis_service importable")
            self.pass_check("build_open_trade_analysis() callable")
        except ImportError as e:
            self.fail_check(f"trade_analysis_service import failed: {e}")
            return
        except Exception as e:
            self.fail_check(f"trade_analysis_service error: {e}")
            return

        # Check canonical_renderer importable
        try:
            from core.analysis.canonical_renderer import render_for_channel

            self.pass_check("canonical_renderer importable")
        except ImportError as e:
            self.warn_check(f"canonical_renderer import failed: {e}")
        except Exception as e:
            self.warn_check(f"canonical_renderer error: {e}")

        # Check analysis_contract
        try:
            from core.analysis.analysis_contract import stamp_version

            self.pass_check("analysis_contract.stamp_version() callable")
        except ImportError as e:
            self.warn_check(f"analysis_contract import failed: {e}")
        except Exception as e:
            self.warn_check(f"analysis_contract error: {e}")

        # Check analysis_metrics
        try:
            from core.analysis.analysis_metrics import inc, get

            self.pass_check("analysis_metrics.inc() callable")
            self.pass_check("analysis_metrics.get() callable")
        except ImportError as e:
            self.warn_check(f"analysis_metrics import failed: {e}")
        except Exception as e:
            self.warn_check(f"analysis_metrics error: {e}")

        # Check FilterStatsTracker
        try:
            from core.analytics.filter_stats import get_filter_stats_tracker
            fst = get_filter_stats_tracker()
            assert hasattr(fst, "record_trade_outcome")
            self.pass_check("FilterStatsTracker.record_trade_outcome() callable")
        except ImportError as e:
            self.warn_check(f"FilterStatsTracker import failed: {e}")
        except Exception as e:
            self.warn_check(f"FilterStatsTracker error: {e}")

    # =========================================================================
    # SECTION 5: PROPOSAL SYSTEM
    # =========================================================================

    def check_proposal_system(self):
        """Check proposal generation and backtest runner."""
        self.print_section("Section 5: Proposal System")

        # Check tuning_proposal_generator
        try:
            from core.analysis.tuning_proposal_generator import generate_tuning_proposals
            self.pass_check("tuning_proposal_generator importable")
        except ImportError as e:
            self.warn_check(f"tuning_proposal_generator import failed: {e}")
        except Exception as e:
            self.warn_check(f"tuning_proposal_generator error: {e}")

        # Check backtest_runner
        try:
            from core.analysis.backtest_runner import run_proposal_backtest, BacktestRunnerError
            self.pass_check("backtest_runner importable")
        except ImportError as e:
            self.warn_check(f"backtest_runner import failed: {e}")
        except Exception as e:
            self.warn_check(f"backtest_runner error: {e}")

        # Check load_pending_proposals
        try:
            from core.analysis.backtest_gating import load_pending_proposals
            proposals = load_pending_proposals()
            self.pass_check(
                f"load_pending_proposals() callable ({len(proposals)} pending)"
            )
        except ImportError as e:
            self.warn_check(f"load_pending_proposals import failed: {e}")
        except Exception as e:
            self.warn_check(f"load_pending_proposals() error: {e}")

        # Check AdaptiveLearningPolicy
        try:
            from core.monitoring.paper_trading_monitor import AdaptiveLearningPolicy

            policy = AdaptiveLearningPolicy()
            self.pass_check("AdaptiveLearningPolicy importable and instantiable")
        except ImportError as e:
            self.fail_check(f"AdaptiveLearningPolicy import failed: {e}")
        except Exception as e:
            self.fail_check(f"AdaptiveLearningPolicy error: {e}")

    # =========================================================================
    # SECTION 6: MONITORING
    # =========================================================================

    def check_monitoring(self):
        """Check monitoring and reporting modules."""
        self.print_section("Section 6: Monitoring")

        # Check MilestoneTracker
        try:
            from core.monitoring.paper_trading_monitor import MilestoneTracker

            tracker = MilestoneTracker()
            self.pass_check("MilestoneTracker importable and instantiable")
        except ImportError as e:
            self.fail_check(f"MilestoneTracker import failed: {e}")
        except Exception as e:
            self.fail_check(f"MilestoneTracker error: {e}")

        # Check LiveReadinessEvaluator
        try:
            from core.monitoring.paper_trading_monitor import LiveReadinessEvaluator

            evaluator = LiveReadinessEvaluator()
            self.pass_check("LiveReadinessEvaluator importable and instantiable")
        except ImportError as e:
            self.fail_check(f"LiveReadinessEvaluator import failed: {e}")
        except Exception as e:
            self.fail_check(f"LiveReadinessEvaluator error: {e}")

        # Check daily_report exists
        daily_report_path = _PROJECT_ROOT / "scripts" / "daily_report.py"
        if daily_report_path.exists():
            self.pass_check("scripts/daily_report.py exists")
        else:
            self.fail_check("scripts/daily_report.py not found")

    # =========================================================================
    # SECTION 7: NOTIFICATION
    # =========================================================================

    def check_notification(self):
        """Check notification manager."""
        self.print_section("Section 7: Notification")

        # Check notification_manager
        try:
            from core.notifications.notification_manager import (
                notification_manager,
            )

            if hasattr(notification_manager, "notify"):
                self.pass_check("NotificationManager has 'notify' method")
            else:
                self.warn_check("notification_manager imported but 'notify' method not found")
        except ImportError as e:
            self.fail_check(f"notification_manager import failed: {e}")
        except Exception as e:
            self.fail_check(f"notification_manager error: {e}")

    # =========================================================================
    # SECTION 8: AUDIT TRAIL
    # =========================================================================

    def check_audit_trail(self):
        """Check audit trail constants and versioning."""
        self.print_section("Section 8: Audit Trail")

        # Check analysis_metrics constants
        try:
            from core.analysis.analysis_metrics import (
                C_APPLIED_CHANGE,
                C_ANALYSIS_ERROR,
                C_ANALYSIS_OK,
            )

            self.pass_check(
                f"analysis_metrics constants present (OK, ERROR, APPLIED_CHANGE)"
            )
        except ImportError as e:
            self.warn_check(f"analysis_metrics constants import failed: {e}")
        except Exception as e:
            self.warn_check(f"analysis_metrics constants error: {e}")

        # Check analysis_contract VERSION
        try:
            from core.analysis.analysis_contract import VERSION

            if VERSION == "2.0":
                self.pass_check(f"analysis_contract VERSION = {VERSION} (correct)")
            else:
                self.warn_check(f"analysis_contract VERSION = {VERSION} (expected 2.0)")
        except ImportError as e:
            self.warn_check(f"analysis_contract VERSION import failed: {e}")
        except Exception as e:
            self.warn_check(f"analysis_contract VERSION error: {e}")

    # =========================================================================
    # MAIN RUN
    # =========================================================================

    def run_all_checks(self) -> int:
        """Run all checks and return exit code."""
        print(f"\n{BOLD}NexusTrader — Pre-Launch Validation Checklist{RESET}")
        print(f"Date: 2026-03-25\n")

        self.check_config()
        self.check_database()
        self.check_data_feeds()
        self.check_analysis_pipeline()
        self.check_proposal_system()
        self.check_monitoring()
        self.check_notification()
        self.check_audit_trail()

        return self.print_summary()


def main():
    """Entry point."""
    runner = ChecklistRunner()
    exit_code = runner.run_all_checks()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
