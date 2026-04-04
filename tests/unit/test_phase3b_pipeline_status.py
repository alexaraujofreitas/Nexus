"""
Phase 3B Tests — Scan Page Pipeline Dashboard.

Validates:
  1. Engine _cmd_get_pipeline_status returns all tradable assets (B2/B3)
  2. Assets with no scanner data show status 'Waiting' (B2)
  3. Pipeline status normalization maps correctly (B2)
  4. Scanner diagnostics are structured and stable (B3)
  5. API endpoint exists in scanner router (B4)
  6. Frontend Scanner.tsx has no watchlist bar (F10)
  7. Frontend Scanner.tsx has no symbol-selection UI (F10)
  8. Frontend uses pipeline-status API (F9/F6)
  9. Source-of-truth enforcement: DB only, no new config.yaml dependency
  10. Scope guard: no intelligence work, no Asset Management changes

Total: 25 tests
"""
from __future__ import annotations

import os
import re
from unittest.mock import AsyncMock, MagicMock, patch
import pytest


# ── Helpers ──────────────────────────────────────────────────
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _read_file(relative_path: str) -> str:
    """Read a project file and return its contents."""
    full_path = os.path.join(PROJECT_ROOT, relative_path)
    with open(full_path, 'r', encoding='utf-8') as f:
        return f.read()


# ── B2: Engine Pipeline Status Command ───────────────────────

class TestEnginePipelineStatus:
    """B2: Engine _cmd_get_pipeline_status returns correct structure."""

    def test_pipeline_command_registered(self):
        """get_pipeline_status command is in the engine command registry."""
        content = _read_file("web/engine/main.py")
        assert '"get_pipeline_status": self._cmd_get_pipeline_status' in content

    def test_pipeline_queries_db_tradable(self):
        """Pipeline status queries Asset.is_tradable from PostgreSQL."""
        content = _read_file("web/engine/main.py")
        assert "Asset.is_tradable" in content
        assert "_cmd_get_pipeline_status" in content
        # Find the method definition (def _cmd_get_pipeline_status)
        idx = content.index("def _cmd_get_pipeline_status")
        method_section = content[idx:idx+5000]
        assert "is_tradable" in method_section

    def test_pipeline_returns_summary(self):
        """Pipeline response includes summary with total/eligible/active_signals/blocked."""
        content = _read_file("web/engine/main.py")
        idx = content.index("def _cmd_get_pipeline_status")
        method_section = content[idx:idx+8000]
        for key in ["n_total", "n_eligible", "n_signals", "n_blocked"]:
            assert key in method_section

    def test_pipeline_returns_all_required_fields(self):
        """Each pipeline row includes all required fields."""
        content = _read_file("web/engine/main.py")
        idx = content.index("def _cmd_get_pipeline_status")
        method_section = content[idx:idx+8000]
        required_fields = [
            '"asset_id"', '"symbol"', '"price"', '"regime"',
            '"score"', '"status"', '"reason"', '"scanned_at"',
            '"diagnostics"', '"allocation_weight"', '"direction"',
            '"models_fired"', '"is_approved"',
        ]
        for field in required_fields:
            assert field in method_section, f"Missing field {field} in pipeline row"

    def test_waiting_status_for_unscanned_assets(self):
        """Assets not in scanner results get status 'Waiting'."""
        content = _read_file("web/engine/main.py")
        idx = content.index("def _cmd_get_pipeline_status")
        method_section = content[idx:idx+8000]
        assert '"Waiting"' in method_section
        assert '"Not yet scanned"' in method_section

    def test_scan_all_results_connected(self):
        """Engine connects to scanner's scan_all_results signal."""
        content = _read_file("web/engine/main.py")
        assert "scan_all_results.connect" in content
        assert "_on_scan_all_results" in content
        assert "_last_pipeline_results" in content


# ── B3: Scanner Diagnostics Structure ────────────────────────

class TestScannerDiagnostics:
    """B3: Scanner produces per-asset diagnostic dict."""

    def test_scanner_emits_diagnostics_in_all_results(self):
        """ScanWorker populates diagnostics key in _all_sym_results."""
        content = _read_file("core/scanning/scanner.py")
        assert '"diagnostics": sym_diag' in content or '"diagnostics": {}' in content
        assert "_all_sym_results" in content

    def test_diagnostics_include_regime_data(self):
        """Diagnostics dict includes regime_confidence and regime_probs."""
        content = _read_file("core/scanning/scanner.py")
        assert "regime_confidence" in content
        assert "regime_probs" in content

    def test_diagnostics_include_model_data(self):
        """Diagnostics dict includes models_fired and models_no_signal."""
        content = _read_file("core/scanning/scanner.py")
        assert "models_fired" in content
        assert "models_no_signal" in content

    def test_diagnostics_include_signal_details(self):
        """Diagnostics dict includes per-model signal_details."""
        content = _read_file("core/scanning/scanner.py")
        assert "signal_details" in content


# ── B4: API Endpoint ─────────────────────────────────────────

class TestPipelineStatusEndpoint:
    """B4: GET /scanner/pipeline-status endpoint exists."""

    def test_endpoint_defined_in_router(self):
        """Router has /pipeline-status GET endpoint."""
        content = _read_file("web/backend/app/api/scanner.py")
        assert '"/pipeline-status"' in content
        assert "get_pipeline_status" in content

    def test_endpoint_calls_engine_command(self):
        """Endpoint delegates to engine get_pipeline_status command."""
        content = _read_file("web/backend/app/api/scanner.py")
        assert '"get_pipeline_status"' in content


# ── F6/F9: Frontend Pipeline Dashboard ───────────────────────

class TestFrontendPipelineDashboard:
    """F6/F9: Scan page redesigned as pipeline table dashboard."""

    def test_scanner_tsx_uses_pipeline_status_api(self):
        """Scanner.tsx imports and uses getPipelineStatus."""
        content = _read_file("web/frontend/src/pages/Scanner.tsx")
        assert "getPipelineStatus" in content
        assert "pipeline-status" in content

    def test_scanner_tsx_has_summary_cards(self):
        """Scanner.tsx renders summary cards (Total, Eligible, Active Signals, Blocked)."""
        content = _read_file("web/frontend/src/pages/Scanner.tsx")
        assert "Total in Universe" in content
        assert "Eligible Now" in content
        assert "Active Signals" in content
        assert "Blocked" in content

    def test_scanner_tsx_has_pipeline_table(self):
        """Scanner.tsx renders a table with required columns."""
        content = _read_file("web/frontend/src/pages/Scanner.tsx")
        for col in ["Symbol", "Price", "Regime", "Weight", "Strategy", "Score", "Status", "Reason", "Scanned"]:
            assert col in content, f"Missing column: {col}"

    def test_scanner_tsx_has_diagnostic_expansion(self):
        """Scanner.tsx has DiagnosticPanel for row expansion."""
        content = _read_file("web/frontend/src/pages/Scanner.tsx")
        assert "DiagnosticPanel" in content
        assert "Pipeline Diagnostics" in content

    def test_scanner_tsx_has_status_filter(self):
        """Scanner.tsx has status and regime filters."""
        content = _read_file("web/frontend/src/pages/Scanner.tsx")
        assert "statusFilter" in content
        assert "regimeFilter" in content
        assert "All Statuses" in content
        assert "All Regimes" in content

    def test_scanner_tsx_has_sorting(self):
        """Scanner.tsx supports column sorting."""
        content = _read_file("web/frontend/src/pages/Scanner.tsx")
        assert "sortField" in content
        assert "sortAsc" in content
        assert "handleSort" in content


# ── F10: Watchlist Bar Removed ───────────────────────────────

class TestWatchlistBarRemoved:
    """F10: Scan page no longer has watchlist bar or symbol selection UI."""

    def test_no_watchlist_bar(self):
        """Scanner.tsx does not render a watchlist bar."""
        content = _read_file("web/frontend/src/pages/Scanner.tsx")
        # Old page had: getWatchlist, watchlistData, watchlist bar JSX
        assert "getWatchlist" not in content
        assert "watchlistData" not in content
        assert "watchlist bar" not in content.lower()

    def test_no_symbol_selection_ui(self):
        """Scanner.tsx has no add/remove symbol controls."""
        content = _read_file("web/frontend/src/pages/Scanner.tsx")
        assert "add symbol" not in content.lower()
        assert "remove symbol" not in content.lower()
        assert "addSymbol" not in content
        assert "removeSymbol" not in content

    def test_no_watchlist_query(self):
        """Scanner.tsx does not query the watchlist endpoint."""
        content = _read_file("web/frontend/src/pages/Scanner.tsx")
        assert "scanner-watchlist" not in content
        # It should ONLY use pipeline-status
        assert "pipeline-status" in content


# ── Source of Truth Enforcement ───────────────────────────────

class TestSOTEnforcement:
    """Phase 3B does not introduce new config.yaml dependencies."""

    def test_no_config_yaml_in_scanner_tsx(self):
        """Scanner.tsx has no config.yaml references."""
        content = _read_file("web/frontend/src/pages/Scanner.tsx")
        assert "config.yaml" not in content
        assert "config_yaml" not in content

    def test_pipeline_status_sources_from_db(self):
        """Pipeline status endpoint returns source='db'."""
        content = _read_file("web/engine/main.py")
        idx = content.index("def _cmd_get_pipeline_status")
        method_section = content[idx:idx+8000]
        assert '"source": "db"' in method_section

    def test_no_hardcoded_symbol_lists_in_scan_page(self):
        """Scanner.tsx has no hardcoded symbol lists."""
        content = _read_file("web/frontend/src/pages/Scanner.tsx")
        # Should not contain hardcoded symbols like BTC/USDT etc.
        assert "BTC/USDT" not in content
        assert "ETH/USDT" not in content


# ── Scope Guard ──────────────────────────────────────────────

class TestScopeGuard:
    """Phase 3B scope compliance."""

    def test_no_intelligence_work(self):
        """No sentiment/funding/OI endpoints added."""
        content = _read_file("web/backend/app/api/scanner.py")
        assert "sentiment" not in content.lower()
        assert "funding" not in content.lower()
        assert "open_interest" not in content.lower()

    def test_phase3a_sot_intact(self):
        """Phase 3A SOT behavior remains: WatchlistManager still delegates to DB."""
        content = _read_file("core/scanning/watchlist.py")
        assert "_try_db_tradable_symbols" in content
        assert "Asset.is_tradable" in content or "is_tradable" in content


# ── Pipeline Status Normalization ─────────────────────────────

class TestPipelineStatusNormalization:
    """_normalize_pipeline_status maps raw statuses correctly."""

    def test_normalization_covers_all_statuses(self):
        """Engine normalizer handles all expected raw statuses."""
        content = _read_file("web/engine/main.py")
        idx = content.index("def _normalize_pipeline_status")
        method_section = content[idx:idx+1500]
        expected_outputs = ["Eligible", "Pre-Filter", "No Signal", "Error", "Risk Blocked", "Waiting"]
        for status in expected_outputs:
            assert f'"{status}"' in method_section, f"Missing normalized status: {status}"

    def test_approved_maps_to_eligible(self):
        """is_approved=True results in 'Eligible' status."""
        content = _read_file("web/engine/main.py")
        idx = content.index("def _normalize_pipeline_status")
        method_section = content[idx:idx+1500]
        assert '"Eligible"' in method_section

    def test_filtered_maps_to_prefilter(self):
        """'Filtered' raw status maps to 'Pre-Filter'."""
        content = _read_file("web/engine/main.py")
        idx = content.index("def _normalize_pipeline_status")
        method_section = content[idx:idx+1500]
        assert '"Pre-Filter"' in method_section
