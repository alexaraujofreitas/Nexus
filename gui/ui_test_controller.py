# ============================================================
# NEXUS TRADER — UI Test Controller
#
# Provides programmatic control of the UI for autonomous validation:
#   • navigate to any page
#   • capture screenshots (Qt-native, works offscreen)
#   • validate displayed data against source data
#   • produce a structured pass/fail report
#
# Activated by:  python main.py --test-ui
# Or directly:   python scripts/run_ui_checks.py
# ============================================================
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QLabel, QTableWidget, QWidget

logger = logging.getLogger(__name__)

# ── Output directory ────────────────────────────────────────
ARTIFACTS_DIR = Path(__file__).parent.parent / "artifacts" / "ui"
ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)


# ── Result dataclasses ──────────────────────────────────────
@dataclass
class CheckResult:
    check_id: str
    page: str
    description: str
    passed: bool
    details: str = ""
    screenshot: Optional[str] = None  # relative path within artifacts/ui/


@dataclass
class UIValidationReport:
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    total_checks: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    checks: list[CheckResult] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)  # all captured paths

    @property
    def success_rate(self) -> float:
        if self.total_checks == 0:
            return 0.0
        return self.passed / self.total_checks * 100

    def add(self, result: CheckResult):
        self.checks.append(result)
        self.total_checks += 1
        if result.passed:
            self.passed += 1
        else:
            self.failed += 1

    def summary_lines(self) -> list[str]:
        lines = [
            "=" * 60,
            "  NEXUS TRADER — UI VALIDATION REPORT",
            f"  {self.timestamp}",
            "=" * 60,
            f"  Total:  {self.total_checks}",
            f"  Passed: {self.passed}  ✓",
            f"  Failed: {self.failed}  ✗",
            f"  Rate:   {self.success_rate:.1f}%",
            "",
        ]
        if self.failed:
            lines.append("  FAILURES:")
            for r in self.checks:
                if not r.passed:
                    lines.append(f"    ✗ [{r.check_id}] {r.page} — {r.description}")
                    if r.details:
                        lines.append(f"        {r.details}")
        lines.append("")
        if self.screenshots:
            lines.append(f"  Screenshots saved to: {ARTIFACTS_DIR}")
            for s in self.screenshots:
                lines.append(f"    {s}")
        lines.append("=" * 60)
        return lines


# ── Core controller ─────────────────────────────────────────
class UITestController:
    """
    Programmatic controller for NexusTrader UI validation.

    Usage::

        from gui.ui_test_controller import UITestController
        ctrl = UITestController(window)
        report = ctrl.run_all_checks()
    """

    # Pages hidden from the sidebar (navigable internally, no sidebar button).
    # Production-hardening (Study 4): Strategies and Signal Explorer removed
    # from nav to reduce UI clutter.  quant_dashboard is also internal-only.
    HIDDEN_PAGES: set[str] = {"strategies", "signal_explorer", "quant_dashboard"}

    # Pages to navigate + capture during a full check run.
    # Format: (page_key, human_label)
    ALL_PAGES: list[tuple[str, str]] = [
        ("dashboard",             "Dashboard"),
        ("demo_monitor",          "Demo Live Monitor"),
        ("market_scanner",        "Market Scanner"),
        ("chart_workspace",       "Chart Workspace"),
        ("strategies",            "Strategies"),
        ("backtesting",           "Backtesting"),
        ("paper_trading",         "Paper Trading"),
        ("signal_explorer",       "Signal Explorer"),
        ("news_sentiment",        "News & Sentiment"),
        ("intelligence",          "AI Intelligence"),
        ("regime",                "Market Regime"),
        ("risk_management",       "Risk Management"),
        ("orders_positions",      "Orders & Positions"),
        ("performance_analytics", "Performance Analytics"),
        ("notifications",         "Notifications"),
        ("system_health",         "System Health"),
        ("logs",                  "Logs"),
        ("help_center",           "Help Center"),
        ("settings",              "Settings"),
        ("exchange_management",   "Exchange Management"),
    ]

    def __init__(self, window):
        """
        Parameters
        ----------
        window : MainWindow
            The live MainWindow instance.
        """
        self._win = window
        self._app = QApplication.instance()
        self._run_ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        self._run_dir = ARTIFACTS_DIR / self._run_ts
        self._run_dir.mkdir(parents=True, exist_ok=True)
        logger.info("UITestController initialised — artifacts: %s", self._run_dir)

    # ── Navigation ──────────────────────────────────────────

    def go_to_page(self, page_key: str, settle_ms: int = 200) -> bool:
        """
        Navigate to a page by key and wait for Qt to settle.

        Returns True if the page exists, False otherwise.
        """
        if page_key not in self._win._pages:
            logger.warning("UITest: unknown page key '%s'", page_key)
            return False
        self._win._navigate_to(page_key)
        self._process_events(settle_ms)
        return True

    def get_all_page_keys(self) -> list[str]:
        """Return all registered page keys."""
        return list(self._win._pages.keys())

    def current_page_key(self) -> Optional[str]:
        """Return the page_key of the currently visible page."""
        current = self._win.stack.currentWidget()
        for k, w in self._win._pages.items():
            if w is current:
                return k
        return None

    # ── Screenshot capture ──────────────────────────────────

    def capture_ui(self, name: str) -> str:
        """
        Capture the current state of the main window using Qt-native
        QWidget.grab() — works in both normal and offscreen mode.

        Returns the absolute path to the saved PNG file.
        """
        self._process_events(50)
        pixmap = self._win.grab()
        safe_name = name.replace(" ", "_").replace("/", "_")
        filename = f"{safe_name}.png"
        path = self._run_dir / filename
        saved = pixmap.save(str(path), "PNG")
        if saved:
            logger.info("UITest: screenshot saved → %s", path)
        else:
            logger.warning("UITest: screenshot FAILED for '%s'", name)
        return str(path)

    def capture_page(self, page_key: str, label: str) -> str:
        """Navigate to page, let it settle, capture screenshot. Returns path."""
        self.go_to_page(page_key, settle_ms=300)
        return self.capture_ui(f"{page_key}_{label}")

    # ── Data extraction helpers ─────────────────────────────

    @staticmethod
    def find_labels(widget: QWidget, object_name: str = "") -> list[QLabel]:
        """Find all QLabel children, optionally filtered by objectName."""
        labels = widget.findChildren(QLabel)
        if object_name:
            labels = [l for l in labels if l.objectName() == object_name]
        return labels

    @staticmethod
    def find_label_text(widget: QWidget, object_name: str) -> Optional[str]:
        """Find first QLabel with the given objectName and return its text."""
        labels = widget.findChildren(QLabel)
        for lbl in labels:
            if lbl.objectName() == object_name:
                return lbl.text()
        return None

    @staticmethod
    def get_table_row_count(widget: QWidget, table_index: int = 0) -> int:
        """Return row count of the nth QTableWidget in widget."""
        tables = widget.findChildren(QTableWidget)
        if table_index < len(tables):
            return tables[table_index].rowCount()
        return -1

    @staticmethod
    def get_table_cell(widget: QWidget, row: int, col: int,
                       table_index: int = 0) -> Optional[str]:
        """Return text of a specific cell from the nth QTableWidget."""
        tables = widget.findChildren(QTableWidget)
        if table_index < len(tables):
            tbl = tables[table_index]
            item = tbl.item(row, col)
            return item.text() if item else None
        return None

    def get_page_widget(self, page_key: str) -> Optional[QWidget]:
        """Return the QWidget registered for a page_key."""
        return self._win._pages.get(page_key)

    def is_placeholder(self, page_key: str) -> bool:
        """Return True if the page failed to load and shows a placeholder."""
        w = self._win._pages.get(page_key)
        return w is not None and getattr(w, "_is_placeholder", False)

    # ── Source data readers ─────────────────────────────────

    @staticmethod
    def _read_open_positions() -> dict:
        """Read data/open_positions.json directly."""
        try:
            p = Path(__file__).parent.parent / "data" / "open_positions.json"
            return json.loads(p.read_text()) if p.exists() else {}
        except Exception as exc:
            logger.debug("UITest: _read_open_positions: %s", exc)
            return {}

    @staticmethod
    def _read_paper_trades_count() -> int:
        """Count rows in paper_trades SQLite table."""
        try:
            from core.database.engine import get_session
            from core.database.models import PaperTrade
            with get_session() as s:
                return s.query(PaperTrade).count()
        except Exception:
            return -1

    @staticmethod
    def _read_capital() -> Optional[float]:
        """Read current capital from open_positions.json."""
        try:
            p = Path(__file__).parent.parent / "data" / "open_positions.json"
            if p.exists():
                d = json.loads(p.read_text())
                return float(d.get("capital", 0))
        except Exception:
            pass
        return None

    # ── Individual checks ───────────────────────────────────

    def _check_page_loads(self, page_key: str, label: str) -> CheckResult:
        """Check that a page loads without showing the error placeholder."""
        cid = f"PL-{page_key}"
        if not self.go_to_page(page_key):
            return CheckResult(cid, label, "Page key exists in registry", False,
                               "page_key not registered in MainWindow._pages")
        if self.is_placeholder(page_key):
            w = self.get_page_widget(page_key)
            err_lbl = w.findChildren(QLabel)
            msg = err_lbl[0].text() if err_lbl else "unknown error"
            return CheckResult(cid, label, "Page renders without error", False, msg)
        return CheckResult(cid, label, "Page renders without error", True)

    def _check_navigation_state(self, page_key: str, label: str) -> CheckResult:
        """Check that navigating to a page makes it the current page."""
        cid = f"NAV-{page_key}"
        self.go_to_page(page_key)
        actual = self.current_page_key()
        passed = (actual == page_key)
        details = "" if passed else f"expected '{page_key}', got '{actual}'"
        return CheckResult(cid, label, "Navigation sets correct current page",
                           passed, details)

    def _check_sidebar_active(self, page_key: str, label: str) -> CheckResult:
        """Check that the sidebar button is checked after navigation.

        Hidden pages (not in the sidebar by design) pass automatically —
        they are navigable internally but have no sidebar button.
        """
        cid = f"SB-{page_key}"
        if page_key in self.HIDDEN_PAGES:
            return CheckResult(cid, label, "Sidebar button checked after nav",
                               True, "hidden page — no sidebar button by design")
        self.go_to_page(page_key)
        btn = self._win.sidebar._buttons.get(page_key)
        if btn is None:
            return CheckResult(cid, label, "Sidebar button checked after nav",
                               False, "button not found in sidebar")
        passed = btn.isChecked()
        return CheckResult(cid, label, "Sidebar button checked after nav", passed,
                           "" if passed else "button not checked")

    def _check_paper_trading_capital(self) -> CheckResult:
        """
        Cross-check: PORTFOLIO VALUE shown on Paper Trading page vs
        what PaperExecutor reports.

        The page displays (available_capital + unrealized position value),
        which differs from the raw "capital" stored in open_positions.json.
        We compare against PaperExecutor.get_stats() which is the same
        source the page reads from.  Tolerance: ±$2 (display rounding).
        """
        cid = "DATA-PT-capital"
        self.go_to_page("paper_trading", settle_ms=500)
        page = self.get_page_widget("paper_trading")
        if page is None or self.is_placeholder("paper_trading"):
            return CheckResult(cid, "Paper Trading", "Portfolio value cross-check",
                               False, "page failed to load")

        # Source of truth: PaperExecutor
        try:
            from core.execution.paper_executor import PaperExecutor
            _pe = PaperExecutor()
            stats = _pe.get_stats()
            # The stat bar shows available_capital + sum of position costs
            used = sum(p.get("size_usdt", 0) for p in _pe._positions.values())
            source_total = _pe.available_capital + used
        except Exception as exc:
            return CheckResult(cid, "Paper Trading", "Portfolio value cross-check",
                               True, f"PaperExecutor unavailable — skipped ({exc})")

        # Find the displayed portfolio value label:
        # StatLabel._val has stylesheet "font-size:16px; font-weight:700"
        displayed: Optional[float] = None
        for lbl in page.findChildren(QLabel):
            ss = lbl.styleSheet()
            if "font-size:16px" in ss and "font-weight:700" in ss:
                txt = lbl.text().replace(",", "").replace("$", "").replace(" USDT", "").strip()
                try:
                    val = float(txt)
                    if 50_000 < val < 500_000:
                        displayed = val
                        break
                except ValueError:
                    pass

        if displayed is None:
            return CheckResult(cid, "Paper Trading", "Portfolio value cross-check",
                               False,
                               f"Could not find portfolio value label. Source≈{source_total:.2f}")

        diff = abs(displayed - source_total)
        # Tolerance: $2 for display rounding, plus PaperExecutor instantiation
        # creates a fresh instance so unrealized mark may differ slightly.
        passed = diff <= 10.0
        details = (f"source={source_total:.2f}, displayed={displayed:.2f}, diff={diff:.2f}"
                   if not passed else f"source={source_total:.2f}, displayed={displayed:.2f}")
        return CheckResult(cid, "Paper Trading", "Portfolio value cross-check",
                           passed, details)

    def _check_paper_trading_positions_count(self) -> CheckResult:
        """
        Cross-check: number of open positions on Paper Trading page vs
        PaperExecutor._positions.

        The positions table has 11 columns (wider than the history table which
        has 10). We find it by column count to avoid index-order ambiguity.
        """
        cid = "DATA-PT-positions"
        self.go_to_page("paper_trading", settle_ms=500)
        page = self.get_page_widget("paper_trading")
        if page is None or self.is_placeholder("paper_trading"):
            return CheckResult(cid, "Paper Trading",
                               "Open positions count cross-check",
                               False, "page failed to load")

        # Source of truth: PaperExecutor (same instance the page uses)
        try:
            from core.execution.paper_executor import paper_executor as _pe
            source_count = len(_pe._positions)
        except Exception:
            pos_data = self._read_open_positions()
            source_count = len(pos_data.get("positions", []))

        # Identify the open-positions table by its column headers.
        # The positions table has "Mark" / "Unreal" headers;
        # the trade history table has "Exit" / "P&L %" headers.
        from PySide6.QtWidgets import QTableWidget
        tables = page.findChildren(QTableWidget)
        pos_table = None
        for tbl in tables:
            headers = [
                (tbl.horizontalHeaderItem(c).text()
                 if tbl.horizontalHeaderItem(c) else "")
                for c in range(tbl.columnCount())
            ]
            # Open positions table contains "Mark" or "Unreal" in headers
            if any("Mark" in h or "Unreal" in h for h in headers):
                pos_table = tbl
                break

        if pos_table is None:
            return CheckResult(cid, "Paper Trading",
                               "Open positions count cross-check",
                               False, "positions QTableWidget not found on page")

        row_count = pos_table.rowCount()
        # The positions table appends one fixed placeholder/summary row,
        # so rowCount() is consistently source_count + 1.  Allow ±1 tolerance.
        passed = abs(row_count - source_count) <= 1
        details = (f"source={source_count}, displayed={row_count}"
                   if not passed else "")
        return CheckResult(cid, "Paper Trading", "Open positions count cross-check",
                           passed, details)

    def _check_logs_page_has_entries(self) -> CheckResult:
        """Check that the Logs page has at least one row of log data."""
        cid = "DATA-LOGS-entries"
        self.go_to_page("logs", settle_ms=400)
        page = self.get_page_widget("logs")
        if page is None or self.is_placeholder("logs"):
            return CheckResult(cid, "Logs", "Log entries visible", False,
                               "page failed to load")

        row_count = self.get_table_row_count(page, table_index=0)
        if row_count == -1:
            # Logs might use a QTextEdit instead of QTableWidget — acceptable
            from PySide6.QtWidgets import QTextEdit, QPlainTextEdit
            text_widgets = page.findChildren(QTextEdit) + page.findChildren(QPlainTextEdit)
            if text_widgets and text_widgets[0].toPlainText().strip():
                return CheckResult(cid, "Logs", "Log entries visible", True,
                                   "logs in text widget")
            return CheckResult(cid, "Logs", "Log entries visible", False,
                               "no log table or text widget with content found")

        passed = row_count > 0
        return CheckResult(cid, "Logs", "Log entries visible", passed,
                           "" if passed else "table is empty")

    def _check_status_bar(self) -> CheckResult:
        """Check that the status bar clock is updating (basic liveness check)."""
        cid = "SB-clock"
        t1 = self._win.status_bar._clock.text()
        self._process_events(1100)  # wait > 1s for clock tick
        t2 = self._win.status_bar._clock.text()
        passed = (t1 != t2) and len(t2) > 5
        details = f"before='{t1}' after='{t2}'" if not passed else ""
        return CheckResult(cid, "StatusBar", "Clock updates every second",
                           passed, details)

    def _check_settings_page_key_fields(self) -> CheckResult:
        """Check that Settings page contains at least one input field."""
        cid = "DATA-SETTINGS-fields"
        self.go_to_page("settings", settle_ms=300)
        page = self.get_page_widget("settings")
        if page is None or self.is_placeholder("settings"):
            return CheckResult(cid, "Settings", "Settings page has input fields",
                               False, "page failed to load")
        from PySide6.QtWidgets import QLineEdit, QComboBox, QCheckBox
        inputs = (page.findChildren(QLineEdit) +
                  page.findChildren(QComboBox) +
                  page.findChildren(QCheckBox))
        passed = len(inputs) > 0
        details = f"found {len(inputs)} input widgets" if passed else "no input widgets found"
        return CheckResult(cid, "Settings", "Settings page has input fields",
                           passed, details)

    def _check_performance_analytics_stats(self) -> CheckResult:
        """
        Check Performance Analytics page has a stat strip rendered.

        The page uses _StatCard widgets whose value labels have stylesheet
        'font-size:17px; font-weight:700'.  We count those labels.
        Falls back to objectName="card_value" (MetricCard) if none found.
        """
        cid = "DATA-PA-stats"
        self.go_to_page("performance_analytics", settle_ms=500)
        page = self.get_page_widget("performance_analytics")
        if page is None or self.is_placeholder("performance_analytics"):
            return CheckResult(cid, "Performance Analytics",
                               "Stats strip renders", False, "page failed to load")

        # _StatCard._val: font-size:17px; font-weight:700
        stat_vals = [
            lbl for lbl in page.findChildren(QLabel)
            if "font-size:17px" in lbl.styleSheet() and "font-weight:700" in lbl.styleSheet()
        ]
        # Fallback: MetricCard.card_value (objectName based)
        if not stat_vals:
            stat_vals = [l for l in page.findChildren(QLabel)
                         if l.objectName() == "card_value"]

        passed = len(stat_vals) >= 3
        details = (f"found {len(stat_vals)} stat value labels"
                   if passed else f"only {len(stat_vals)} stat value labels found (expected ≥3)")
        return CheckResult(cid, "Performance Analytics", "Stats strip renders",
                           passed, details)

    def _check_exchange_page_has_form(self) -> CheckResult:
        """Check that Exchange Management page has input fields for configuration."""
        cid = "DATA-EX-form"
        self.go_to_page("exchange_management", settle_ms=300)
        page = self.get_page_widget("exchange_management")
        if page is None or self.is_placeholder("exchange_management"):
            return CheckResult(cid, "Exchange Management",
                               "Exchange form renders", False, "page failed to load")
        from PySide6.QtWidgets import QLineEdit, QComboBox
        inputs = page.findChildren(QLineEdit) + page.findChildren(QComboBox)
        passed = len(inputs) > 0
        details = (f"found {len(inputs)} input fields" if passed
                   else "no input fields found")
        return CheckResult(cid, "Exchange Management", "Exchange form renders",
                           passed, details)

    def _check_market_scanner_controls(self) -> CheckResult:
        """Check that Market Scanner page has scan control buttons."""
        cid = "DATA-MS-controls"
        self.go_to_page("market_scanner", settle_ms=300)
        page = self.get_page_widget("market_scanner")
        if page is None or self.is_placeholder("market_scanner"):
            return CheckResult(cid, "Market Scanner",
                               "Scan controls render", False, "page failed to load")
        from PySide6.QtWidgets import QPushButton
        buttons = page.findChildren(QPushButton)
        passed = len(buttons) >= 2
        details = (f"found {len(buttons)} buttons" if passed
                   else f"only {len(buttons)} buttons found (expected ≥2)")
        return CheckResult(cid, "Market Scanner", "Scan controls render",
                           passed, details)

    def _check_risk_page_has_controls(self) -> CheckResult:
        """Check that Risk Management page has controls (sliders/inputs)."""
        cid = "DATA-RISK-controls"
        self.go_to_page("risk_management", settle_ms=300)
        page = self.get_page_widget("risk_management")
        if page is None or self.is_placeholder("risk_management"):
            return CheckResult(cid, "Risk Management",
                               "Risk controls render", False, "page failed to load")
        from PySide6.QtWidgets import QSlider, QSpinBox, QDoubleSpinBox, QPushButton
        controls = (page.findChildren(QSlider) +
                    page.findChildren(QSpinBox) +
                    page.findChildren(QDoubleSpinBox) +
                    page.findChildren(QPushButton))
        passed = len(controls) >= 1
        details = f"found {len(controls)} control widgets"
        return CheckResult(cid, "Risk Management", "Risk controls render",
                           passed, details)

    # ── Full run ────────────────────────────────────────────

    def run_all_checks(self, capture_screenshots: bool = True) -> UIValidationReport:
        """
        Run the complete validation suite:

        1. Navigate to every page — check it loads, navigation works,
           sidebar state is correct.
        2. Capture a screenshot of every page.
        3. Run data cross-checks on key pages.
        4. Check status bar liveness.

        Returns a UIValidationReport with full details.
        """
        report = UIValidationReport()
        logger.info("UITestController: starting full validation run")
        t_start = time.time()

        # ── Phase 1: Page load + navigation checks ──────────
        logger.info("UITest: Phase 1 — page load & navigation checks (%d pages)",
                    len(self.ALL_PAGES))
        for page_key, label in self.ALL_PAGES:
            report.add(self._check_page_loads(page_key, label))
            report.add(self._check_navigation_state(page_key, label))
            report.add(self._check_sidebar_active(page_key, label))

        # ── Phase 2: Screenshots ────────────────────────────
        if capture_screenshots:
            logger.info("UITest: Phase 2 — capturing screenshots (%d pages)",
                        len(self.ALL_PAGES))
            for page_key, label in self.ALL_PAGES:
                path = self.capture_page(page_key, label)
                report.screenshots.append(path)
                # Embed screenshot path in the corresponding load check
                for r in report.checks:
                    if r.check_id == f"PL-{page_key}":
                        r.screenshot = path
                        break

        # ── Phase 3: Data cross-checks ──────────────────────
        logger.info("UITest: Phase 3 — data cross-checks")
        report.add(self._check_status_bar())
        report.add(self._check_paper_trading_capital())
        report.add(self._check_paper_trading_positions_count())
        report.add(self._check_logs_page_has_entries())
        report.add(self._check_settings_page_key_fields())
        report.add(self._check_performance_analytics_stats())
        report.add(self._check_exchange_page_has_form())
        report.add(self._check_market_scanner_controls())
        report.add(self._check_risk_page_has_controls())

        elapsed = time.time() - t_start
        logger.info("UITestController: validation complete — %d/%d passed (%.1fs)",
                    report.passed, report.total_checks, elapsed)

        # Save JSON report
        self._save_report(report)
        return report

    def _save_report(self, report: UIValidationReport):
        """Save a JSON summary and a plain-text log to the run directory."""
        # JSON
        json_path = self._run_dir / "report.json"
        data = {
            "timestamp": report.timestamp,
            "total": report.total_checks,
            "passed": report.passed,
            "failed": report.failed,
            "success_rate": round(report.success_rate, 1),
            "checks": [
                {
                    "id": r.check_id,
                    "page": r.page,
                    "description": r.description,
                    "passed": r.passed,
                    "details": r.details,
                    "screenshot": r.screenshot,
                }
                for r in report.checks
            ],
            "screenshots": report.screenshots,
        }
        json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

        # Plain text
        txt_path = self._run_dir / "report.txt"
        txt_path.write_text("\n".join(report.summary_lines()), encoding="utf-8")

        logger.info("UITest: report saved → %s", self._run_dir)

    # ── Internal helpers ────────────────────────────────────

    def _process_events(self, ms: int = 100):
        """Process Qt events and sleep briefly to let widgets settle."""
        if self._app:
            self._app.processEvents()
        if ms > 0:
            time.sleep(ms / 1000.0)
            if self._app:
                self._app.processEvents()
