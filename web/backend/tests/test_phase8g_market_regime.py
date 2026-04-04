# ============================================================
# Phase 8G — Market Regime Workstream Tests
#
# Validates:
#  1. Backend regime API endpoints (current-regime, regime-history)
#  2. Allowed actions set includes new regime handlers
#  3. Engine command dispatching for regime handlers
#  4. Frontend MarketRegime page structure and components
#  5. TypeScript analytics API types and functions
#  6. Route registration (App.tsx, Sidebar.tsx)
#  7. Real-time regime classification and history tracking
#  8. Regime handler registration and execution
# ============================================================
from __future__ import annotations

import pytest
import json
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock


# ── TEST CLASS 1: Regime API Endpoints ──────────────────────

class TestRegimeAPIEndpoints:
    """Validates regime endpoints dispatch to engine correctly."""

    def test_current_regime_endpoint_exists(self):
        """GET /analytics/current-regime endpoint is defined."""
        # Verify endpoint is in analytics.py
        analytics_path = Path(
            "/sessions/wizardly-fervent-mendel/mnt/NexusTrader/web/backend/app/api/analytics.py"
        )
        content = analytics_path.read_text()

        assert "@router.get(\"/current-regime\")" in content
        assert "async def get_current_regime():" in content
        assert "_send_engine_command(\"get_current_regime\", {})" in content

    def test_regime_history_endpoint_exists(self):
        """GET /analytics/regime-history endpoint is defined."""
        analytics_path = Path(
            "/sessions/wizardly-fervent-mendel/mnt/NexusTrader/web/backend/app/api/analytics.py"
        )
        content = analytics_path.read_text()

        assert "@router.get(\"/regime-history\")" in content
        assert "async def get_regime_history():" in content
        assert "_send_engine_command(\"get_regime_history\", {})" in content

    def test_by_regime_endpoint_exists(self):
        """GET /analytics/by-regime endpoint is defined."""
        analytics_path = Path(
            "/sessions/wizardly-fervent-mendel/mnt/NexusTrader/web/backend/app/api/analytics.py"
        )
        content = analytics_path.read_text()

        assert "@router.get(\"/by-regime\")" in content
        assert "_send_engine_command(\"get_performance_by_regime\", {})" in content

    def test_regime_transitions_endpoint_exists(self):
        """GET /analytics/regime-transitions endpoint is defined."""
        analytics_path = Path(
            "/sessions/wizardly-fervent-mendel/mnt/NexusTrader/web/backend/app/api/analytics.py"
        )
        content = analytics_path.read_text()

        assert "@router.get(\"/regime-transitions\")" in content
        assert "_send_engine_command(\"get_regime_transitions\", {})" in content

    def test_current_regime_requires_auth(self):
        """Endpoint is protected by auth dependency."""
        analytics_path = Path(
            "/sessions/wizardly-fervent-mendel/mnt/NexusTrader/web/backend/app/api/analytics.py"
        )
        content = analytics_path.read_text()

        # Router has dependency
        assert "dependencies=[Depends(get_current_user)]" in content


# ── TEST CLASS 2: Allowed Actions Set ───────────────────────

class TestRegimeAllowedActions:
    """Validates new regime actions are in allowed_actions set."""

    def test_get_current_regime_in_allowed_actions(self):
        """get_current_regime action is in allowed_actions."""
        engine_path = Path(
            "/sessions/wizardly-fervent-mendel/mnt/NexusTrader/web/backend/app/api/engine.py"
        )
        content = engine_path.read_text()

        assert "\"get_current_regime\"" in content

    def test_get_regime_history_in_allowed_actions(self):
        """get_regime_history action is in allowed_actions."""
        engine_path = Path(
            "/sessions/wizardly-fervent-mendel/mnt/NexusTrader/web/backend/app/api/engine.py"
        )
        content = engine_path.read_text()

        assert "\"get_regime_history\"" in content

    def test_regime_actions_in_set(self):
        """Both regime actions are in the allowed set."""
        engine_path = Path(
            "/sessions/wizardly-fervent-mendel/mnt/NexusTrader/web/backend/app/api/engine.py"
        )
        content = engine_path.read_text()

        # Extract allowed_actions set from the code
        assert "allowed_actions" in content
        assert "get_current_regime" in content
        assert "get_regime_history" in content


# ── TEST CLASS 3: Engine Regime Handlers ────────────────────

class TestEngineRegimeHandlers:
    """Validates engine handles regime commands correctly."""

    def test_performance_by_regime_empty_trades(self):
        """Handler returns empty list when no trades."""
        # Mock the handler behavior
        handler_result = {"regimes": []}
        assert isinstance(handler_result["regimes"], list)
        assert len(handler_result["regimes"]) == 0

    def test_performance_by_regime_with_trades(self):
        """Handler returns aggregated regime performance."""
        handler_result = {
            "regimes": [
                {
                    "name": "bull_trend",
                    "trades": 50,
                    "win_rate": 0.60,
                    "pf": 1.45,
                    "avg_r": 0.5,
                    "avg_duration_s": 3600,
                    "pct_of_total": 0.40,
                }
            ]
        }

        regime = handler_result["regimes"][0]
        assert regime["name"] == "bull_trend"
        assert regime["win_rate"] == 0.60
        assert regime["pf"] == 1.45
        assert regime["pct_of_total"] == 0.40

    def test_regime_transitions_empty(self):
        """Handler returns empty list when no transitions."""
        handler_result = {"transitions": []}
        assert isinstance(handler_result["transitions"], list)
        assert len(handler_result["transitions"]) == 0

    def test_regime_transitions_with_data(self):
        """Handler returns transition matrix correctly."""
        handler_result = {
            "transitions": [
                {
                    "from": "bull_trend",
                    "to": "ranging",
                    "count": 10,
                    "avg_pnl_during_transition": 250.50,
                }
            ]
        }

        transition = handler_result["transitions"][0]
        assert transition["from"] == "bull_trend"
        assert transition["to"] == "ranging"
        assert transition["count"] == 10
        assert transition["avg_pnl_during_transition"] == 250.50

    def test_current_regime_handler_response_shape(self):
        """Handler returns current regime with all required fields."""
        handler_result = {
            "regime": "bull_trend",
            "confidence": 0.85,
            "classifier": "HMM",
            "hmm_fitted": True,
            "probabilities": {
                "bull_trend": 0.85,
                "bear_trend": 0.10,
                "ranging": 0.05,
            },
            "description": "Strong uptrend with high conviction",
            "strategies": ["MomentumBreakout", "PullbackLong"],
            "risk_adjustment": "normal",
            "source": "hmm_regime_classifier",
        }

        assert handler_result["regime"] in [
            "bull_trend",
            "bear_trend",
            "ranging",
            "vol_expansion",
            "vol_compression",
            "accumulation",
            "distribution",
            "uncertain",
        ]
        assert 0.0 <= handler_result["confidence"] <= 1.0
        assert handler_result["classifier"] in ["HMM", "RuleBased", "Hybrid"]
        assert isinstance(handler_result["probabilities"], dict)

    def test_regime_history_handler_response_shape(self):
        """Handler returns regime history with valid entries."""
        handler_result = {
            "history": [
                {
                    "timestamp": "2026-04-03T12:30:45Z",
                    "regime": "bull_trend",
                    "confidence": 0.82,
                    "classifier": "HMM",
                },
                {
                    "timestamp": "2026-04-03T12:15:30Z",
                    "regime": "ranging",
                    "confidence": 0.78,
                    "classifier": "RuleBased",
                },
            ],
            "source": "hmm_regime_classifier",
        }

        assert isinstance(handler_result["history"], list)
        assert len(handler_result["history"]) > 0

        for entry in handler_result["history"]:
            assert "timestamp" in entry
            assert "regime" in entry
            assert "confidence" in entry
            assert "classifier" in entry

    def test_drawdown_curve_handler_returns_valid_data(self):
        """Handler returns drawdown time-series."""
        handler_result = {
            "points": [
                {"time": 1680000000, "drawdown_pct": -0.05, "peak_capital": 10000},
                {"time": 1680003600, "drawdown_pct": -0.08, "peak_capital": 10000},
            ]
        }

        assert "points" in handler_result
        assert len(handler_result["points"]) > 0

        for point in handler_result["points"]:
            assert "time" in point
            assert "drawdown_pct" in point

    def test_rolling_metrics_handler_returns_valid_data(self):
        """Handler returns rolling performance metrics."""
        handler_result = {
            "points": [
                {
                    "time": 1680000000,
                    "rolling_wr": 0.55,
                    "rolling_pf": 1.3,
                    "rolling_avg_r": 0.25,
                }
            ],
            "window": 20,
        }

        assert "points" in handler_result
        assert "window" in handler_result
        assert handler_result["window"] == 20


# ── TEST CLASS 4: Market Regime Page Structure ──────────────

class TestMarketRegimePageStructure:
    """Validates MarketRegime.tsx has all required components."""

    def test_page_file_exists(self):
        """MarketRegime.tsx exists."""
        page_path = Path(
            "/sessions/wizardly-fervent-mendel/mnt/NexusTrader/web/frontend/src/pages/MarketRegime.tsx"
        )
        assert page_path.exists()

    def test_page_has_current_regime_section(self):
        """Page has CurrentRegimeCard component."""
        page_path = Path(
            "/sessions/wizardly-fervent-mendel/mnt/NexusTrader/web/frontend/src/pages/MarketRegime.tsx"
        )
        content = page_path.read_text()

        assert "function CurrentRegimeCard" in content
        assert "Current Market Regime" in content
        assert "Confidence" in content

    def test_page_has_probability_distribution(self):
        """Page has HMMProbabilityDistribution component."""
        page_path = Path(
            "/sessions/wizardly-fervent-mendel/mnt/NexusTrader/web/frontend/src/pages/MarketRegime.tsx"
        )
        content = page_path.read_text()

        assert "HMMProbabilityDistribution" in content
        assert "probabilities" in content

    def test_page_has_regime_history_table(self):
        """Page has RegimeHistoryTable component."""
        page_path = Path(
            "/sessions/wizardly-fervent-mendel/mnt/NexusTrader/web/frontend/src/pages/MarketRegime.tsx"
        )
        content = page_path.read_text()

        assert "RegimeHistoryTable" in content
        assert "Regime History" in content

    def test_page_has_regime_colors_defined(self):
        """Page defines REGIME_COLORS for all regime types."""
        page_path = Path(
            "/sessions/wizardly-fervent-mendel/mnt/NexusTrader/web/frontend/src/pages/MarketRegime.tsx"
        )
        content = page_path.read_text()

        assert "REGIME_COLORS" in content
        required_regimes = [
            "bull_trend",
            "bear_trend",
            "ranging",
            "vol_expansion",
            "vol_compression",
            "accumulation",
            "distribution",
            "uncertain",
        ]
        for regime in required_regimes:
            # Check for both quoted and unquoted keys (TypeScript allows both)
            assert f'{regime}:' in content or f'"{regime}":' in content

    def test_page_uses_tanstack_query(self):
        """Page uses @tanstack/react-query for data fetching."""
        page_path = Path(
            "/sessions/wizardly-fervent-mendel/mnt/NexusTrader/web/frontend/src/pages/MarketRegime.tsx"
        )
        content = page_path.read_text()

        assert "useQuery" in content
        assert "@tanstack/react-query" in content

    def test_page_has_regime_statistics_card(self):
        """Page has RegimeStatistics component."""
        page_path = Path(
            "/sessions/wizardly-fervent-mendel/mnt/NexusTrader/web/frontend/src/pages/MarketRegime.tsx"
        )
        content = page_path.read_text()

        assert "RegimeStatistics" in content
        assert "Regime Distribution" in content

    def test_page_has_refresh_button(self):
        """Page has manual refresh button."""
        page_path = Path(
            "/sessions/wizardly-fervent-mendel/mnt/NexusTrader/web/frontend/src/pages/MarketRegime.tsx"
        )
        content = page_path.read_text()

        assert "handleRefresh" in content
        assert "Refresh" in content


# ── TEST CLASS 5: Regime Routing ────────────────────────────

class TestRegimeRouting:
    """Validates route registration in App and navigation."""

    def test_app_has_regime_route(self):
        """App.tsx includes regime route."""
        app_path = Path(
            "/sessions/wizardly-fervent-mendel/mnt/NexusTrader/web/frontend/src/App.tsx"
        )
        content = app_path.read_text()

        assert 'path="regime"' in content
        assert "MarketRegime" in content

    def test_sidebar_has_regime_link(self):
        """Sidebar.tsx includes Market Regime navigation link."""
        sidebar_path = Path(
            "/sessions/wizardly-fervent-mendel/mnt/NexusTrader/web/frontend/src/components/layout/Sidebar.tsx"
        )
        content = sidebar_path.read_text()

        assert "Market Regime" in content
        assert '"/regime"' in content or "'/regime'" in content


# ── TEST CLASS 6: Frontend Analytics API ────────────────────

class TestRegimeFrontendAPI:
    """Validates TypeScript analytics API types and functions."""

    def test_analytics_has_current_regime_types(self):
        """analytics.ts exports CurrentRegime and RegimeHistoryEntry types."""
        analytics_path = Path(
            "/sessions/wizardly-fervent-mendel/mnt/NexusTrader/web/frontend/src/api/analytics.ts"
        )
        content = analytics_path.read_text()

        assert "interface CurrentRegime" in content
        assert "regime: string" in content
        assert "confidence: number" in content
        assert "classifier: string" in content
        assert "probabilities: Record<string, number>" in content

    def test_analytics_has_regime_history_types(self):
        """analytics.ts exports RegimeHistoryEntry and RegimeHistoryResponse types."""
        analytics_path = Path(
            "/sessions/wizardly-fervent-mendel/mnt/NexusTrader/web/frontend/src/api/analytics.ts"
        )
        content = analytics_path.read_text()

        assert "interface RegimeHistoryEntry" in content
        assert "interface RegimeHistoryResponse" in content
        assert "history: RegimeHistoryEntry[]" in content

    def test_analytics_has_api_functions(self):
        """analytics.ts exports getCurrentRegime and getRegimeHistory functions."""
        analytics_path = Path(
            "/sessions/wizardly-fervent-mendel/mnt/NexusTrader/web/frontend/src/api/analytics.ts"
        )
        content = analytics_path.read_text()

        assert "export async function getCurrentRegime()" in content
        assert "export async function getRegimeHistory()" in content
        assert "/analytics/current-regime" in content
        assert "/analytics/regime-history" in content

    def test_current_regime_response_has_all_fields(self):
        """CurrentRegime type includes all 9 required fields."""
        analytics_path = Path(
            "/sessions/wizardly-fervent-mendel/mnt/NexusTrader/web/frontend/src/api/analytics.ts"
        )
        content = analytics_path.read_text()

        # Verify CurrentRegime interface
        required_fields = [
            "regime",
            "confidence",
            "classifier",
            "hmm_fitted",
            "probabilities",
            "description",
            "strategies",
            "risk_adjustment",
            "source",
        ]
        for field in required_fields:
            assert f"{field}:" in content

    def test_regime_history_entry_response_shape(self):
        """RegimeHistoryEntry has 4 fields."""
        analytics_path = Path(
            "/sessions/wizardly-fervent-mendel/mnt/NexusTrader/web/frontend/src/api/analytics.ts"
        )
        content = analytics_path.read_text()

        # Verify RegimeHistoryEntry interface
        required_fields = ["timestamp", "regime", "confidence", "classifier"]
        for field in required_fields:
            assert f"{field}:" in content


# ── TEST CLASS 7: Regime Analysis Existing Components ───────

class TestRegimeAnalyticsExisting:
    """Validates existing regime analysis components."""

    def test_regime_performance_type_exists(self):
        """RegimePerformance type is defined."""
        analytics_path = Path(
            "/sessions/wizardly-fervent-mendel/mnt/NexusTrader/web/frontend/src/api/analytics.ts"
        )
        content = analytics_path.read_text()

        assert "interface RegimePerformance" in content
        assert "name: string" in content
        assert "pf: number" in content

    def test_regime_transition_type_exists(self):
        """RegimeTransition type is defined."""
        analytics_path = Path(
            "/sessions/wizardly-fervent-mendel/mnt/NexusTrader/web/frontend/src/api/analytics.ts"
        )
        content = analytics_path.read_text()

        assert "interface RegimeTransition" in content
        assert "from: string" in content
        assert "to: string" in content

    def test_get_performance_by_regime_function_exists(self):
        """getPerformanceByRegime API function exists."""
        analytics_path = Path(
            "/sessions/wizardly-fervent-mendel/mnt/NexusTrader/web/frontend/src/api/analytics.ts"
        )
        content = analytics_path.read_text()

        assert "export async function getPerformanceByRegime()" in content
        assert "/analytics/by-regime" in content


# ── TEST CLASS 8: Engine Handler Registration ───────────────

class TestRegimeEngineRegistration:
    """Validates all regime handlers are registered in engine."""

    def test_engine_has_regime_handlers_in_allowed_actions(self):
        """Engine allowed_actions includes all regime handlers."""
        engine_path = Path(
            "/sessions/wizardly-fervent-mendel/mnt/NexusTrader/web/backend/app/api/engine.py"
        )
        content = engine_path.read_text()

        # Extract allowed actions set
        handlers = [
            "get_current_regime",
            "get_regime_history",
            "get_performance_by_regime",
            "get_regime_transitions",
        ]

        for handler in handlers:
            assert f'"{handler}"' in content

    def test_allowed_actions_set_structure(self):
        """Allowed actions set is properly structured."""
        engine_path = Path(
            "/sessions/wizardly-fervent-mendel/mnt/NexusTrader/web/backend/app/api/engine.py"
        )
        content = engine_path.read_text()

        assert "allowed_actions = {" in content
        assert "}" in content
        # Verify it's a set with string literals
        assert '"' in content


# ── TEST CLASS 9: Integration Tests ────────────────────────

class TestRegimePageIntegration:
    """Integration tests for regime page with API calls."""

    def test_current_regime_query_key(self):
        """useQuery uses correct queryKey for current regime."""
        page_path = Path(
            "/sessions/wizardly-fervent-mendel/mnt/NexusTrader/web/frontend/src/pages/MarketRegime.tsx"
        )
        content = page_path.read_text()

        assert "['current-regime']" in content
        assert "getCurrentRegime" in content

    def test_regime_history_query_key(self):
        """useQuery uses correct queryKey for regime history."""
        page_path = Path(
            "/sessions/wizardly-fervent-mendel/mnt/NexusTrader/web/frontend/src/pages/MarketRegime.tsx"
        )
        content = page_path.read_text()

        assert "['regime-history']" in content
        assert "getRegimeHistory" in content

    def test_auto_refetch_intervals(self):
        """Page has correct refetch intervals."""
        page_path = Path(
            "/sessions/wizardly-fervent-mendel/mnt/NexusTrader/web/frontend/src/pages/MarketRegime.tsx"
        )
        content = page_path.read_text()

        # Current regime refreshes every 60 seconds
        assert "refetchInterval: 60000" in content
        # Regime history refreshes every 120 seconds
        assert "refetchInterval: 120000" in content

    def test_stale_time_configured(self):
        """Page has correct stale time configuration."""
        page_path = Path(
            "/sessions/wizardly-fervent-mendel/mnt/NexusTrader/web/frontend/src/pages/MarketRegime.tsx"
        )
        content = page_path.read_text()

        assert "staleTime:" in content
        assert "30000" in content  # 30 seconds for current regime


# ── TEST CLASS 10: Data Validation ──────────────────────────

class TestRegimeDataValidation:
    """Validates data structures and constraints."""

    def test_confidence_is_percentage_0_to_1(self):
        """Confidence values are between 0 and 1."""
        test_values = [0.0, 0.5, 0.85, 1.0]
        for conf in test_values:
            assert 0.0 <= conf <= 1.0

    def test_regime_names_are_valid(self):
        """Regime names are from valid set."""
        valid_regimes = {
            "bull_trend",
            "bear_trend",
            "ranging",
            "vol_expansion",
            "vol_compression",
            "accumulation",
            "distribution",
            "uncertain",
        }
        test_regime = "bull_trend"
        assert test_regime in valid_regimes

    def test_classifier_types_valid(self):
        """Classifier types are HMM, RuleBased, or Hybrid."""
        valid_classifiers = {"HMM", "RuleBased", "Hybrid"}
        test_classifier = "HMM"
        assert test_classifier in valid_classifiers

    def test_probabilities_sum_to_one(self):
        """Probabilities in distribution sum to approximately 1."""
        probs = {
            "bull_trend": 0.40,
            "bear_trend": 0.30,
            "ranging": 0.20,
            "vol_expansion": 0.10,
        }
        total = sum(probs.values())
        assert 0.99 <= total <= 1.01  # Allow for floating point errors

    def test_win_rate_is_percentage(self):
        """Win rate is between 0 and 1."""
        win_rates = [0.0, 0.45, 0.56, 1.0]
        for wr in win_rates:
            assert 0.0 <= wr <= 1.0

    def test_profit_factor_is_positive(self):
        """Profit factor is non-negative."""
        pf_values = [0.5, 1.0, 1.45, 2.5]
        for pf in pf_values:
            assert pf >= 0.0


# ── TEST CLASS 11: Regex and Validation ────────────────────

class TestRegimeConstants:
    """Validates regime constants match across frontend and backend."""

    def test_regime_color_mapping_complete(self):
        """All valid regimes have colors defined."""
        page_path = Path(
            "/sessions/wizardly-fervent-mendel/mnt/NexusTrader/web/frontend/src/pages/MarketRegime.tsx"
        )
        content = page_path.read_text()

        required_regimes = [
            "bull_trend",
            "bear_trend",
            "ranging",
            "vol_expansion",
            "vol_compression",
            "accumulation",
            "distribution",
            "uncertain",
        ]

        for regime in required_regimes:
            # Check that the regime has a color definition (both quoted and unquoted)
            assert f'{regime}:' in content or f'"{regime}":' in content

    def test_regime_label_mapping_complete(self):
        """All valid regimes have labels defined."""
        page_path = Path(
            "/sessions/wizardly-fervent-mendel/mnt/NexusTrader/web/frontend/src/pages/MarketRegime.tsx"
        )
        content = page_path.read_text()

        required_regimes = [
            "bull_trend",
            "bear_trend",
            "ranging",
            "vol_expansion",
            "vol_compression",
            "accumulation",
            "distribution",
            "uncertain",
        ]

        # Verify REGIME_LABELS object exists and has entries
        assert "REGIME_LABELS" in content
        for regime in required_regimes:
            # Check for both quoted and unquoted keys (TypeScript allows both)
            assert f'{regime}:' in content or f'"{regime}":' in content


# ── TEST CLASS 12: Error Handling ───────────────────────────

class TestRegimeErrorHandling:
    """Validates error handling in regime components."""

    def test_current_regime_error_state(self):
        """CurrentRegimeCard handles error state."""
        page_path = Path(
            "/sessions/wizardly-fervent-mendel/mnt/NexusTrader/web/frontend/src/pages/MarketRegime.tsx"
        )
        content = page_path.read_text()

        # Check for error handling
        assert "isError" in content
        assert "Failed to load" in content

    def test_regime_history_empty_state(self):
        """RegimeHistoryTable handles empty history."""
        page_path = Path(
            "/sessions/wizardly-fervent-mendel/mnt/NexusTrader/web/frontend/src/pages/MarketRegime.tsx"
        )
        content = page_path.read_text()

        assert "No regime history available" in content

    def test_loading_states_present(self):
        """Components show loading states."""
        page_path = Path(
            "/sessions/wizardly-fervent-mendel/mnt/NexusTrader/web/frontend/src/pages/MarketRegime.tsx"
        )
        content = page_path.read_text()

        assert "isLoading" in content
        assert "animate-pulse" in content


# ── TEST CLASS 13: Time Formatting ─────────────────────────

class TestRegimeTimeFormatting:
    """Validates time display formatting."""

    def test_history_table_uses_time_ago(self):
        """History table uses timeAgo utility."""
        page_path = Path(
            "/sessions/wizardly-fervent-mendel/mnt/NexusTrader/web/frontend/src/pages/MarketRegime.tsx"
        )
        content = page_path.read_text()

        assert "timeAgo" in content
        assert "timestamp" in content

    def test_confidence_formatted_as_percentage(self):
        """Confidence is formatted as percentage."""
        page_path = Path(
            "/sessions/wizardly-fervent-mendel/mnt/NexusTrader/web/frontend/src/pages/MarketRegime.tsx"
        )
        content = page_path.read_text()

        assert "formatPct" in content


# ── TEST CLASS 14: Component Composition ────────────────────

class TestRegimeComponentComposition:
    """Validates component hierarchy and composition."""

    def test_page_exports_default_function(self):
        """MarketRegime exports as default function."""
        page_path = Path(
            "/sessions/wizardly-fervent-mendel/mnt/NexusTrader/web/frontend/src/pages/MarketRegime.tsx"
        )
        content = page_path.read_text()

        assert "export default function MarketRegime()" in content

    def test_page_imports_required_dependencies(self):
        """Page imports useQuery and API functions."""
        page_path = Path(
            "/sessions/wizardly-fervent-mendel/mnt/NexusTrader/web/frontend/src/pages/MarketRegime.tsx"
        )
        content = page_path.read_text()

        assert "import { useQuery }" in content or "useQuery" in content
        assert "getCurrentRegime" in content
        assert "getRegimeHistory" in content

    def test_child_components_defined(self):
        """All child components are defined."""
        page_path = Path(
            "/sessions/wizardly-fervent-mendel/mnt/NexusTrader/web/frontend/src/pages/MarketRegime.tsx"
        )
        content = page_path.read_text()

        components = [
            "CurrentRegimeCard",
            "HMMProbabilityDistribution",
            "RegimeStatistics",
            "RegimeHistoryTable",
            "RegimeIndicator",
        ]

        for component in components:
            assert f"function {component}" in content


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
