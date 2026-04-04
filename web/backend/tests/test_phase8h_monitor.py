# ============================================================
# Phase 8H — Demo Monitor Workstream Tests
#
# Validates:
#  1. Monitor API endpoints (5 endpoints: positions, portfolio, pnl, risk, trades)
#  2. Allowed actions include monitor commands
#  3. Engine command handlers (5 handlers)
#  4. Handler output schemas and field correctness
#  5. WebSocket channel registration ("monitor")
#  6. Frontend DemoMonitor page structure and components
#  7. TypeScript monitor API types and functions
#  8. Route registration (App.tsx, Sidebar.tsx)
#  9. Error handling and degraded mode
# 10. No mock data — all from PaperExecutor
# 11. Real-time first: WebSocket primary, REST polling fallback
# 12. Execution realism: fees, R-multiples, next-bar semantics
# ============================================================
from __future__ import annotations

import pytest
import json
import re
from pathlib import Path

# ── Paths ────────────────────────────────────────────────
BASE = Path("/sessions/wizardly-fervent-mendel/mnt/NexusTrader/web")
MONITOR_API = BASE / "backend" / "app" / "api" / "monitor.py"
ENGINE_PY = BASE / "engine" / "main.py"
ENGINE_API = BASE / "backend" / "app" / "api" / "engine.py"
WS_MANAGER = BASE / "backend" / "app" / "ws" / "manager.py"
MONITOR_TS = BASE / "frontend" / "src" / "api" / "monitor.ts"
DEMO_MONITOR_TSX = BASE / "frontend" / "src" / "pages" / "DemoMonitor.tsx"
APP_TSX = BASE / "frontend" / "src" / "App.tsx"
SIDEBAR_TSX = BASE / "frontend" / "src" / "components" / "layout" / "Sidebar.tsx"


# ============================================================
# CLASS 1: Monitor API Endpoints
# ============================================================
class TestMonitorAPIEndpoints:
    """Validates all 5 monitor REST endpoints exist and dispatch correctly."""

    def test_positions_endpoint_exists(self):
        content = MONITOR_API.read_text()
        assert '@router.get("/positions")' in content

    def test_portfolio_endpoint_exists(self):
        content = MONITOR_API.read_text()
        assert '@router.get("/portfolio")' in content

    def test_pnl_endpoint_exists(self):
        content = MONITOR_API.read_text()
        assert '@router.get("/pnl")' in content

    def test_risk_endpoint_exists(self):
        content = MONITOR_API.read_text()
        assert '@router.get("/risk")' in content

    def test_trades_endpoint_exists(self):
        content = MONITOR_API.read_text()
        assert '@router.get("/trades")' in content

    def test_positions_dispatches_to_engine(self):
        content = MONITOR_API.read_text()
        assert '_send_engine_command("get_active_positions", {})' in content

    def test_portfolio_dispatches_to_engine(self):
        content = MONITOR_API.read_text()
        assert '_send_engine_command("get_portfolio_state", {})' in content

    def test_pnl_dispatches_to_engine(self):
        content = MONITOR_API.read_text()
        assert '_send_engine_command("get_live_pnl", {})' in content

    def test_risk_dispatches_to_engine(self):
        content = MONITOR_API.read_text()
        assert '_send_engine_command("get_risk_state", {})' in content

    def test_trades_dispatches_to_engine(self):
        content = MONITOR_API.read_text()
        assert '_send_engine_command("get_recent_trades_monitor", {})' in content

    def test_router_prefix_is_monitor(self):
        content = MONITOR_API.read_text()
        assert 'prefix="/monitor"' in content

    def test_router_tag_is_monitor(self):
        content = MONITOR_API.read_text()
        assert 'tags=["monitor"]' in content


# ============================================================
# CLASS 2: Auth Protection
# ============================================================
class TestMonitorAuth:
    """All monitor endpoints require authentication."""

    def test_all_endpoints_require_auth(self):
        content = MONITOR_API.read_text()
        assert "Depends(get_current_user)" in content

    def test_positions_has_user_dependency(self):
        content = MONITOR_API.read_text()
        assert "async def get_monitor_positions(user=Depends(get_current_user))" in content

    def test_portfolio_has_user_dependency(self):
        content = MONITOR_API.read_text()
        assert "async def get_monitor_portfolio(user=Depends(get_current_user))" in content

    def test_pnl_has_user_dependency(self):
        content = MONITOR_API.read_text()
        assert "async def get_monitor_pnl(user=Depends(get_current_user))" in content

    def test_risk_has_user_dependency(self):
        content = MONITOR_API.read_text()
        assert "async def get_monitor_risk(user=Depends(get_current_user))" in content

    def test_trades_has_user_dependency(self):
        content = MONITOR_API.read_text()
        assert "async def get_monitor_trades(user=Depends(get_current_user))" in content


# ============================================================
# CLASS 3: Allowed Actions Whitelist
# ============================================================
class TestAllowedActions:
    """Monitor actions are in the engine command whitelist."""

    def test_get_active_positions_allowed(self):
        content = ENGINE_API.read_text()
        assert '"get_active_positions"' in content

    def test_get_portfolio_state_allowed(self):
        content = ENGINE_API.read_text()
        assert '"get_portfolio_state"' in content

    def test_get_live_pnl_allowed(self):
        content = ENGINE_API.read_text()
        assert '"get_live_pnl"' in content

    def test_get_risk_state_allowed(self):
        content = ENGINE_API.read_text()
        assert '"get_risk_state"' in content

    def test_get_recent_trades_monitor_allowed(self):
        content = ENGINE_API.read_text()
        assert '"get_recent_trades_monitor"' in content


# ============================================================
# CLASS 4: Engine Handler Registration
# ============================================================
class TestEngineHandlerRegistration:
    """All 5 monitor handlers are registered in the dispatch table."""

    def test_active_positions_handler_registered(self):
        content = ENGINE_PY.read_text()
        assert '"get_active_positions": self._cmd_get_active_positions' in content

    def test_portfolio_state_handler_registered(self):
        content = ENGINE_PY.read_text()
        assert '"get_portfolio_state": self._cmd_get_portfolio_state' in content

    def test_live_pnl_handler_registered(self):
        content = ENGINE_PY.read_text()
        assert '"get_live_pnl": self._cmd_get_live_pnl' in content

    def test_risk_state_handler_registered(self):
        content = ENGINE_PY.read_text()
        assert '"get_risk_state": self._cmd_get_risk_state' in content

    def test_recent_trades_monitor_handler_registered(self):
        content = ENGINE_PY.read_text()
        assert '"get_recent_trades_monitor": self._cmd_get_recent_trades_monitor' in content

    def test_handler_methods_defined(self):
        content = ENGINE_PY.read_text()
        for method in [
            "_cmd_get_active_positions",
            "_cmd_get_portfolio_state",
            "_cmd_get_live_pnl",
            "_cmd_get_risk_state",
            "_cmd_get_recent_trades_monitor",
        ]:
            assert f"async def {method}(self, params: dict)" in content


# ============================================================
# CLASS 5: Handler Output Schema — Positions
# ============================================================
class TestPositionsHandlerOutput:
    """Validates _cmd_get_active_positions produces correct schema."""

    def _get_handler_code(self):
        content = ENGINE_PY.read_text()
        start = content.index("async def _cmd_get_active_positions")
        # Find next handler or end
        end = content.index("async def _cmd_get_portfolio_state")
        return content[start:end]

    def test_returns_status_ok(self):
        code = self._get_handler_code()
        assert '"status": "ok"' in code

    def test_returns_positions_list(self):
        code = self._get_handler_code()
        assert '"positions": positions' in code

    def test_returns_count(self):
        code = self._get_handler_code()
        assert '"count": len(positions)' in code

    def test_returns_timestamp(self):
        code = self._get_handler_code()
        assert '"timestamp":' in code

    def test_returns_data_source_paper_executor(self):
        code = self._get_handler_code()
        assert '"data_source": "paper_executor"' in code

    def test_position_has_symbol(self):
        code = self._get_handler_code()
        assert '"symbol"' in code

    def test_position_has_side_normalized(self):
        """Side must be 'long' or 'short', not 'buy'/'sell'."""
        code = self._get_handler_code()
        assert '"long" if side in ("buy", "long") else "short"' in code

    def test_position_has_pnl_unrealized(self):
        code = self._get_handler_code()
        assert '"pnl_unrealized"' in code

    def test_position_has_pnl_pct(self):
        code = self._get_handler_code()
        assert '"pnl_pct"' in code

    def test_position_has_duration_s(self):
        code = self._get_handler_code()
        assert '"duration_s"' in code

    def test_position_has_stop_loss_and_take_profit(self):
        code = self._get_handler_code()
        assert '"stop_loss"' in code
        assert '"take_profit"' in code

    def test_position_has_regime_at_entry(self):
        code = self._get_handler_code()
        assert '"regime_at_entry"' in code

    def test_position_has_models_fired(self):
        code = self._get_handler_code()
        assert '"models_fired"' in code

    def test_position_has_auto_partial_applied(self):
        code = self._get_handler_code()
        assert '"_auto_partial_applied"' in code

    def test_position_has_breakeven_applied(self):
        code = self._get_handler_code()
        assert '"_breakeven_applied"' in code

    def test_reads_from_paper_executor(self):
        """Data comes from self._pe.get_open_positions(), not mock."""
        code = self._get_handler_code()
        assert "self._pe.get_open_positions()" in code

    def test_error_returns_error_status(self):
        code = self._get_handler_code()
        assert '"status": "error"' in code


# ============================================================
# CLASS 6: Handler Output Schema — Portfolio
# ============================================================
class TestPortfolioHandlerOutput:
    """Validates _cmd_get_portfolio_state produces correct schema."""

    def _get_handler_code(self):
        content = ENGINE_PY.read_text()
        start = content.index("async def _cmd_get_portfolio_state")
        end = content.index("async def _cmd_get_live_pnl")
        return content[start:end]

    def test_has_equity(self):
        assert '"equity"' in self._get_handler_code()

    def test_has_balance(self):
        assert '"balance"' in self._get_handler_code()

    def test_has_used_margin(self):
        assert '"used_margin"' in self._get_handler_code()

    def test_has_free_margin(self):
        assert '"free_margin"' in self._get_handler_code()

    def test_has_portfolio_heat_pct(self):
        assert '"portfolio_heat_pct"' in self._get_handler_code()

    def test_has_max_heat_limit(self):
        code = self._get_handler_code()
        assert '"max_heat_limit": 6.0' in code

    def test_has_drawdown_pct(self):
        assert '"drawdown_pct"' in self._get_handler_code()

    def test_has_open_positions_count(self):
        assert '"open_positions"' in self._get_handler_code()

    def test_has_win_rate(self):
        assert '"win_rate"' in self._get_handler_code()

    def test_has_profit_factor(self):
        assert '"profit_factor"' in self._get_handler_code()

    def test_has_trading_paused(self):
        assert '"trading_paused"' in self._get_handler_code()

    def test_reads_from_paper_executor_production_status(self):
        code = self._get_handler_code()
        assert "self._pe.get_production_status()" in code

    def test_reads_from_paper_executor_stats(self):
        code = self._get_handler_code()
        assert "self._pe.get_stats()" in code

    def test_calculates_heat_from_positions(self):
        """Heat % must be computed from actual exposure, not mocked."""
        code = self._get_handler_code()
        assert "total_exposure / capital * 100" in code

    def test_data_source_is_paper_executor(self):
        assert '"data_source": "paper_executor"' in self._get_handler_code()


# ============================================================
# CLASS 7: Handler Output Schema — PnL
# ============================================================
class TestPnLHandlerOutput:
    """Validates _cmd_get_live_pnl produces correct schema."""

    def _get_handler_code(self):
        content = ENGINE_PY.read_text()
        start = content.index("async def _cmd_get_live_pnl")
        end = content.index("async def _cmd_get_risk_state")
        return content[start:end]

    def test_has_total_unrealized(self):
        assert '"total_unrealized"' in self._get_handler_code()

    def test_has_total_realized(self):
        assert '"total_realized"' in self._get_handler_code()

    def test_has_daily_pnl(self):
        assert '"daily_pnl"' in self._get_handler_code()

    def test_has_fees_paid(self):
        assert '"fees_paid"' in self._get_handler_code()

    def test_has_net_pnl(self):
        assert '"net_pnl"' in self._get_handler_code()

    def test_fee_estimation_uses_maker_rate(self):
        """Fees = size × 0.04% × 2 sides = size × 0.0004 × 2."""
        code = self._get_handler_code()
        assert "0.0004 * 2" in code

    def test_net_pnl_includes_fee_deduction(self):
        code = self._get_handler_code()
        assert "total_realized + total_unrealized - fees_paid" in code

    def test_reads_unrealized_from_open_positions(self):
        code = self._get_handler_code()
        assert "self._pe.get_open_positions()" in code

    def test_reads_realized_from_closed_trades(self):
        code = self._get_handler_code()
        assert "_closed_trades" in code


# ============================================================
# CLASS 8: Handler Output Schema — Risk
# ============================================================
class TestRiskHandlerOutput:
    """Validates _cmd_get_risk_state produces correct schema."""

    def _get_handler_code(self):
        content = ENGINE_PY.read_text()
        start = content.index("async def _cmd_get_risk_state")
        end = content.index("async def _cmd_get_recent_trades_monitor")
        return content[start:end]

    def test_has_drawdown_pct(self):
        assert '"drawdown_pct"' in self._get_handler_code()

    def test_has_daily_loss_pct(self):
        assert '"daily_loss_pct"' in self._get_handler_code()

    def test_has_circuit_breaker_triggered(self):
        assert '"circuit_breaker_triggered"' in self._get_handler_code()

    def test_has_trading_enabled(self):
        assert '"trading_enabled"' in self._get_handler_code()

    def test_has_crash_defense_tier(self):
        assert '"crash_defense_tier"' in self._get_handler_code()

    def test_default_tier_is_normal(self):
        code = self._get_handler_code()
        assert '"crash_defense_tier": "NORMAL"' in code

    def test_has_is_defensive(self):
        assert '"is_defensive"' in self._get_handler_code()

    def test_has_is_safe_mode(self):
        assert '"is_safe_mode"' in self._get_handler_code()

    def test_has_reason_field(self):
        assert '"reason"' in self._get_handler_code()

    def test_reads_crash_defense_from_orchestrator(self):
        code = self._get_handler_code()
        assert "self._orch" in code

    def test_circuit_breaker_reason(self):
        code = self._get_handler_code()
        assert '"Circuit breaker triggered"' in code

    def test_trading_paused_reason(self):
        code = self._get_handler_code()
        assert '"Trading manually paused"' in code

    def test_safe_mode_includes_emergency_and_systemic(self):
        code = self._get_handler_code()
        assert '"EMERGENCY"' in code
        assert '"SYSTEMIC"' in code


# ============================================================
# CLASS 9: Handler Output Schema — Recent Trades
# ============================================================
class TestRecentTradesHandlerOutput:
    """Validates _cmd_get_recent_trades_monitor output."""

    def _get_handler_code(self):
        content = ENGINE_PY.read_text()
        start = content.index("async def _cmd_get_recent_trades_monitor")
        # Find next section or method
        end_markers = ["async def _update_state", "# ── State Management"]
        for marker in end_markers:
            if marker in content[start + 10:]:
                end = content.index(marker, start + 10)
                return content[start:end]
        return content[start:]

    def test_has_symbol(self):
        assert '"symbol"' in self._get_handler_code()

    def test_has_side(self):
        assert '"side"' in self._get_handler_code()

    def test_has_entry_price(self):
        assert '"entry_price"' in self._get_handler_code()

    def test_has_exit_price(self):
        assert '"exit_price"' in self._get_handler_code()

    def test_has_pnl_usdt(self):
        assert '"pnl_usdt"' in self._get_handler_code()

    def test_has_r_multiple(self):
        assert '"r_multiple"' in self._get_handler_code()

    def test_has_duration_s(self):
        assert '"duration_s"' in self._get_handler_code()

    def test_has_regime(self):
        assert '"regime"' in self._get_handler_code()

    def test_has_exit_reason(self):
        assert '"exit_reason"' in self._get_handler_code()

    def test_has_fees_estimated(self):
        assert '"fees_estimated"' in self._get_handler_code()

    def test_has_slippage(self):
        assert '"slippage"' in self._get_handler_code()

    def test_has_closed_at(self):
        assert '"closed_at"' in self._get_handler_code()

    def test_has_score(self):
        assert '"score"' in self._get_handler_code()

    def test_r_multiple_calculation_long(self):
        """R = (exit - entry) / risk_per_unit for longs."""
        code = self._get_handler_code()
        assert "(exit_p - entry) / risk_per_unit" in code

    def test_r_multiple_calculation_short(self):
        """R = (entry - exit) / risk_per_unit for shorts."""
        code = self._get_handler_code()
        assert "(entry - exit_p) / risk_per_unit" in code

    def test_fee_estimation_uses_maker_rate(self):
        code = self._get_handler_code()
        assert "0.0004 * 2" in code

    def test_limits_to_50_trades(self):
        code = self._get_handler_code()
        assert "[-50:]" in code

    def test_newest_first_ordering(self):
        code = self._get_handler_code()
        assert "recent.reverse()" in code

    def test_reads_from_closed_trades(self):
        code = self._get_handler_code()
        assert "_closed_trades" in code


# ============================================================
# CLASS 10: WebSocket Channel
# ============================================================
class TestMonitorWebSocketChannel:
    """Monitor channel is registered in WS manager."""

    def test_monitor_channel_in_channels_set(self):
        content = WS_MANAGER.read_text()
        assert '"monitor"' in content

    def test_monitor_channel_comment(self):
        content = WS_MANAGER.read_text()
        assert "Phase 8H" in content


# ============================================================
# CLASS 11: Frontend TypeScript API
# ============================================================
class TestFrontendMonitorAPI:
    """TypeScript monitor API types and functions."""

    def test_monitor_position_interface(self):
        content = MONITOR_TS.read_text()
        assert "export interface MonitorPosition" in content

    def test_portfolio_state_interface(self):
        content = MONITOR_TS.read_text()
        assert "export interface PortfolioState" in content

    def test_live_pnl_interface(self):
        content = MONITOR_TS.read_text()
        assert "export interface LivePnL" in content

    def test_risk_state_interface(self):
        content = MONITOR_TS.read_text()
        assert "export interface RiskState" in content

    def test_monitor_trade_interface(self):
        content = MONITOR_TS.read_text()
        assert "export interface MonitorTrade" in content

    def test_positions_response_wrapper(self):
        content = MONITOR_TS.read_text()
        assert "export interface PositionsResponse" in content

    def test_portfolio_response_wrapper(self):
        content = MONITOR_TS.read_text()
        assert "export interface PortfolioResponse" in content

    def test_pnl_response_wrapper(self):
        content = MONITOR_TS.read_text()
        assert "export interface PnLResponse" in content

    def test_risk_response_wrapper(self):
        content = MONITOR_TS.read_text()
        assert "export interface RiskResponse" in content

    def test_trades_response_wrapper(self):
        content = MONITOR_TS.read_text()
        assert "export interface TradesResponse" in content

    def test_get_monitor_positions_function(self):
        content = MONITOR_TS.read_text()
        assert "export async function getMonitorPositions()" in content

    def test_get_monitor_portfolio_function(self):
        content = MONITOR_TS.read_text()
        assert "export async function getMonitorPortfolio()" in content

    def test_get_monitor_pnl_function(self):
        content = MONITOR_TS.read_text()
        assert "export async function getMonitorPnL()" in content

    def test_get_monitor_risk_function(self):
        content = MONITOR_TS.read_text()
        assert "export async function getMonitorRisk()" in content

    def test_get_monitor_trades_function(self):
        content = MONITOR_TS.read_text()
        assert "export async function getMonitorTrades()" in content

    def test_api_path_positions(self):
        content = MONITOR_TS.read_text()
        assert "'/monitor/positions'" in content

    def test_api_path_portfolio(self):
        content = MONITOR_TS.read_text()
        assert "'/monitor/portfolio'" in content

    def test_api_path_pnl(self):
        content = MONITOR_TS.read_text()
        assert "'/monitor/pnl'" in content

    def test_api_path_risk(self):
        content = MONITOR_TS.read_text()
        assert "'/monitor/risk'" in content

    def test_api_path_trades(self):
        content = MONITOR_TS.read_text()
        assert "'/monitor/trades'" in content


# ============================================================
# CLASS 12: Frontend TypeScript Type Fields
# ============================================================
class TestMonitorTypeFields:
    """Validates TypeScript interface fields match engine handler output."""

    def test_monitor_position_has_pnl_unrealized(self):
        content = MONITOR_TS.read_text()
        assert "pnl_unrealized: number" in content

    def test_monitor_position_has_pnl_pct(self):
        content = MONITOR_TS.read_text()
        assert "pnl_pct: number" in content

    def test_monitor_position_has_duration_s(self):
        content = MONITOR_TS.read_text()
        assert "duration_s: number" in content

    def test_monitor_position_has_regime_at_entry(self):
        content = MONITOR_TS.read_text()
        assert "regime_at_entry: string" in content

    def test_monitor_position_has_auto_partial(self):
        content = MONITOR_TS.read_text()
        assert "_auto_partial_applied: boolean" in content

    def test_monitor_position_has_breakeven(self):
        content = MONITOR_TS.read_text()
        assert "_breakeven_applied: boolean" in content

    def test_risk_state_has_crash_defense_tier(self):
        content = MONITOR_TS.read_text()
        assert "crash_defense_tier: string" in content

    def test_risk_state_has_circuit_breaker(self):
        content = MONITOR_TS.read_text()
        assert "circuit_breaker_triggered: boolean" in content

    def test_live_pnl_has_net_pnl(self):
        content = MONITOR_TS.read_text()
        assert "net_pnl: number" in content

    def test_monitor_trade_has_r_multiple(self):
        content = MONITOR_TS.read_text()
        assert "r_multiple: number" in content

    def test_monitor_trade_has_fees_estimated(self):
        content = MONITOR_TS.read_text()
        assert "fees_estimated: number" in content

    def test_monitor_trade_has_slippage(self):
        content = MONITOR_TS.read_text()
        assert "slippage: number" in content

    def test_positions_response_has_data_source(self):
        content = MONITOR_TS.read_text()
        assert "data_source: string" in content


# ============================================================
# CLASS 13: DemoMonitor Page Structure
# ============================================================
class TestDemoMonitorPageStructure:
    """Validates the 5 mandatory sections exist in DemoMonitor.tsx."""

    def test_page_default_export(self):
        content = DEMO_MONITOR_TSX.read_text()
        assert "export default function DemoMonitor()" in content

    def test_portfolio_summary_component(self):
        content = DEMO_MONITOR_TSX.read_text()
        assert "function PortfolioSummary" in content

    def test_active_positions_table_component(self):
        content = DEMO_MONITOR_TSX.read_text()
        assert "function ActivePositionsTable" in content

    def test_risk_panel_component(self):
        content = DEMO_MONITOR_TSX.read_text()
        assert "function RiskPanel" in content

    def test_recent_trades_table_component(self):
        content = DEMO_MONITOR_TSX.read_text()
        assert "function RecentTradesTable" in content

    def test_regime_overlay_in_risk_panel(self):
        """Regime overlay is section 4 — embedded in RiskPanel."""
        content = DEMO_MONITOR_TSX.read_text()
        assert "Market Regime" in content

    def test_page_title(self):
        content = DEMO_MONITOR_TSX.read_text()
        assert "Demo Monitor" in content

    def test_uses_tanstack_query(self):
        content = DEMO_MONITOR_TSX.read_text()
        assert "useQuery" in content

    def test_uses_ws_store(self):
        content = DEMO_MONITOR_TSX.read_text()
        assert "useWSStore" in content


# ============================================================
# CLASS 14: Real-Time First (WebSocket Primary)
# ============================================================
class TestRealTimeFirst:
    """WebSocket is primary data source, REST is polling fallback."""

    def test_ws_connect_on_mount(self):
        content = DEMO_MONITOR_TSX.read_text()
        assert "connect()" in content

    def test_subscribes_to_positions_channel(self):
        content = DEMO_MONITOR_TSX.read_text()
        assert "subscribe('positions')" in content

    def test_subscribes_to_dashboard_channel(self):
        content = DEMO_MONITOR_TSX.read_text()
        assert "subscribe('dashboard')" in content

    def test_subscribes_to_crash_defense_channel(self):
        content = DEMO_MONITOR_TSX.read_text()
        assert "subscribe('crash_defense')" in content

    def test_subscribes_to_risk_channel(self):
        content = DEMO_MONITOR_TSX.read_text()
        assert "subscribe('risk')" in content

    def test_subscribes_to_monitor_channel(self):
        content = DEMO_MONITOR_TSX.read_text()
        assert "subscribe('monitor')" in content

    def test_ws_data_takes_priority_positions(self):
        """WS lastMessage overrides REST data when available."""
        content = DEMO_MONITOR_TSX.read_text()
        assert "lastMessage['positions']?.positions || positionsData?.positions" in content

    def test_ws_data_takes_priority_portfolio(self):
        content = DEMO_MONITOR_TSX.read_text()
        assert "lastMessage['dashboard'] || portfolioData?.portfolio" in content

    def test_ws_data_takes_priority_risk(self):
        content = DEMO_MONITOR_TSX.read_text()
        assert "lastMessage['risk'] || riskData?.risk" in content

    def test_rest_polling_interval_30s(self):
        content = DEMO_MONITOR_TSX.read_text()
        assert "refetchInterval: 30000" in content

    def test_connection_status_indicator(self):
        """Page shows Live/Connecting/Offline status."""
        content = DEMO_MONITOR_TSX.read_text()
        assert "'Live'" in content or '"Live"' in content
        assert "'Connecting...'" in content or '"Connecting..."' in content
        assert "'Offline'" in content or '"Offline"' in content


# ============================================================
# CLASS 15: Route Registration
# ============================================================
class TestRouteRegistration:
    """DemoMonitor is registered in App.tsx and Sidebar.tsx."""

    def test_app_imports_demo_monitor(self):
        content = APP_TSX.read_text()
        assert "DemoMonitor" in content

    def test_app_has_monitor_route(self):
        content = APP_TSX.read_text()
        assert 'path="monitor"' in content

    def test_sidebar_has_demo_monitor_entry(self):
        content = SIDEBAR_TSX.read_text()
        assert "Demo Monitor" in content

    def test_sidebar_links_to_monitor(self):
        content = SIDEBAR_TSX.read_text()
        assert "/monitor" in content


# ============================================================
# CLASS 16: Execution Realism
# ============================================================
class TestExecutionRealism:
    """Validates fee tracking, R-multiples, and position enrichment."""

    def test_fee_rate_is_maker_0_04_pct(self):
        """0.04%/side = 0.0004 per side."""
        content = ENGINE_PY.read_text()
        # Used in both PnL and trades handlers
        assert "0.0004 * 2" in content

    def test_r_multiple_uses_stop_loss_risk(self):
        """R = price_delta / risk_per_unit where risk = |entry - SL|."""
        content = ENGINE_PY.read_text()
        assert "abs(entry - sl)" in content

    def test_r_multiple_fallback_when_no_stop(self):
        """Fallback: pnl / (size * 0.01) when no SL."""
        content = ENGINE_PY.read_text()
        assert "pnl / (size * 0.01)" in content

    def test_duration_computed_from_opened_at(self):
        """Duration is live-computed from UTC now - opened_at."""
        content = ENGINE_PY.read_text()
        assert "datetime.now(timezone.utc) - dt" in content

    def test_pnl_pct_computed_from_size(self):
        """pnl_pct = unrealized_pnl / size * 100."""
        content = ENGINE_PY.read_text()
        assert "pnl / size * 100" in content


# ============================================================
# CLASS 17: No Mock Data
# ============================================================
class TestNoMockData:
    """All data sourced from PaperExecutor — no mock/placeholder data."""

    def test_no_mock_in_monitor_api(self):
        content = MONITOR_API.read_text()
        assert "mock" not in content.lower()
        assert "placeholder" not in content.lower()
        assert "fake" not in content.lower()

    def test_no_mock_in_monitor_ts(self):
        content = MONITOR_TS.read_text()
        assert "mock" not in content.lower()
        assert "placeholder" not in content.lower()

    def test_no_hardcoded_sample_data_in_page(self):
        content = DEMO_MONITOR_TSX.read_text()
        assert "mock" not in content.lower()
        assert "sample" not in content.lower()
        assert "placeholder" not in content.lower().replace("animate-pulse", "")

    def test_all_handlers_check_self_pe(self):
        """All 5 handlers gate on self._pe existence."""
        content = ENGINE_PY.read_text()
        for handler in [
            "_cmd_get_active_positions",
            "_cmd_get_portfolio_state",
            "_cmd_get_live_pnl",
            "_cmd_get_risk_state",
            "_cmd_get_recent_trades_monitor",
        ]:
            start = content.index(f"async def {handler}")
            end_markers = [
                "async def _cmd_get_portfolio_state",
                "async def _cmd_get_live_pnl",
                "async def _cmd_get_risk_state",
                "async def _cmd_get_recent_trades_monitor",
                "async def _update_state",
            ]
            handler_code = None
            for marker in end_markers:
                idx = content.find(marker, start + 10)
                if idx > start:
                    handler_code = content[start:idx]
                    break
            if handler_code is None:
                handler_code = content[start:]
            assert "self._pe" in handler_code, f"{handler} does not reference self._pe"

    def test_data_source_always_paper_executor(self):
        """All handlers set data_source='paper_executor'."""
        content = ENGINE_PY.read_text()
        # Count occurrences within the 8H handler region
        region_start = content.index("_cmd_get_active_positions")
        region = content[region_start:]
        count = region.count('"data_source": "paper_executor"')
        assert count >= 5, f"Expected 5+ data_source fields, found {count}"


# ============================================================
# CLASS 18: Error Handling / Degraded Mode
# ============================================================
class TestErrorHandling:
    """Handlers return error status on exception, UI shows loading state."""

    def test_positions_handler_has_try_except(self):
        content = ENGINE_PY.read_text()
        start = content.index("async def _cmd_get_active_positions")
        chunk = content[start:start + 2000]
        assert "except Exception" in chunk

    def test_portfolio_handler_has_try_except(self):
        content = ENGINE_PY.read_text()
        start = content.index("async def _cmd_get_portfolio_state")
        chunk = content[start:start + 4000]
        assert "except Exception" in chunk

    def test_pnl_handler_has_try_except(self):
        content = ENGINE_PY.read_text()
        start = content.index("async def _cmd_get_live_pnl")
        chunk = content[start:start + 4000]
        assert "except Exception" in chunk

    def test_risk_handler_has_try_except(self):
        content = ENGINE_PY.read_text()
        start = content.index("async def _cmd_get_risk_state")
        chunk = content[start:start + 4000]
        assert "except Exception" in chunk

    def test_trades_handler_has_try_except(self):
        content = ENGINE_PY.read_text()
        start = content.index("async def _cmd_get_recent_trades_monitor")
        chunk = content[start:start + 4000]
        assert "except Exception" in chunk

    def test_ui_shows_loading_skeleton(self):
        """Portfolio summary shows shimmer skeleton while loading."""
        content = DEMO_MONITOR_TSX.read_text()
        assert "animate-pulse" in content

    def test_ui_shows_no_positions_message(self):
        content = DEMO_MONITOR_TSX.read_text()
        assert "No active positions" in content

    def test_ui_shows_no_trades_message(self):
        content = DEMO_MONITOR_TSX.read_text()
        assert "No recent trades" in content


# ============================================================
# CLASS 19: Desktop Parity — UI Elements
# ============================================================
class TestDesktopParity:
    """Key desktop Demo Monitor elements are present in web version."""

    def test_equity_displayed(self):
        content = DEMO_MONITOR_TSX.read_text()
        assert "'Equity'" in content or '"Equity"' in content

    def test_daily_pnl_displayed(self):
        content = DEMO_MONITOR_TSX.read_text()
        assert "'Daily PnL'" in content or '"Daily PnL"' in content

    def test_unrealized_pnl_displayed(self):
        content = DEMO_MONITOR_TSX.read_text()
        assert "Unrealized PnL" in content

    def test_realized_pnl_displayed(self):
        content = DEMO_MONITOR_TSX.read_text()
        assert "Realized PnL" in content

    def test_portfolio_heat_displayed(self):
        content = DEMO_MONITOR_TSX.read_text()
        assert "Portfolio Heat" in content

    def test_fees_paid_displayed(self):
        content = DEMO_MONITOR_TSX.read_text()
        assert "Fees Paid" in content

    def test_crash_defense_tier_badges(self):
        content = DEMO_MONITOR_TSX.read_text()
        for tier in ["NORMAL", "DEFENSIVE", "HIGH_ALERT", "EMERGENCY", "SYSTEMIC"]:
            assert tier in content

    def test_regime_color_mapping(self):
        content = DEMO_MONITOR_TSX.read_text()
        for regime in ["bull_trend", "bear_trend", "ranging", "vol_expansion"]:
            assert regime in content

    def test_circuit_breaker_badge(self):
        content = DEMO_MONITOR_TSX.read_text()
        assert "Circuit Breaker" in content

    def test_trading_enabled_badge(self):
        content = DEMO_MONITOR_TSX.read_text()
        assert "ENABLED" in content
        assert "DISABLED" in content

    def test_positions_sorted_by_pnl(self):
        content = DEMO_MONITOR_TSX.read_text()
        assert "sort((a, b) => b.pnl_unrealized - a.pnl_unrealized)" in content

    def test_trades_sorted_newest_first(self):
        content = DEMO_MONITOR_TSX.read_text()
        assert "sort((a, b) => new Date(b.closed_at).getTime() - new Date(a.closed_at).getTime())" in content

    def test_pnl_color_coding(self):
        """Positive PnL green, negative red."""
        content = DEMO_MONITOR_TSX.read_text()
        assert "text-green-600" in content
        assert "text-red-600" in content


# ============================================================
# Summary: 19 classes, 150+ tests
# ============================================================
