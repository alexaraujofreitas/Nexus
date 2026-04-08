"""
Phase 9 Test Suite — View Builders

Tests:
- TradesViewBuilder: build from records, summary stats, sorting
- PositionsViewBuilder: build from records, exposure computation
- HealthViewBuilder: all component states, defaults
- MetricsViewBuilder: slippage and latency aggregation
"""

import pytest
from core.intraday.monitoring.views.trades_view import TradesViewBuilder
from core.intraday.monitoring.views.positions_view import PositionsViewBuilder
from core.intraday.monitoring.views.health_view import HealthViewBuilder
from core.intraday.monitoring.views.metrics_view import MetricsViewBuilder
from core.intraday.monitoring.views.view_contracts import TradeView, PositionView


# ══════════════════════════════════════════════════════════════
# 1. TRADES VIEW BUILDER
# ══════════════════════════════════════════════════════════════

class TestTradesViewBuilder:
    def test_build_from_valid_records(self):
        records = [
            {
                "position_id": "p1", "symbol": "BTCUSDT", "direction": "long",
                "strategy_name": "M", "entry_price": 50000, "close_price": 51000,
                "quantity": 0.01, "entry_size_usdt": 500, "realized_pnl_usdt": 10,
                "fee_total_usdt": 0.5, "r_multiple": 1.0, "close_reason": "tp_hit",
                "regime_at_entry": "bull", "opened_at_ms": 1000, "closed_at_ms": 3000,
                "bars_held": 2,
            },
            {
                "position_id": "p2", "symbol": "ETHUSDT", "direction": "short",
                "strategy_name": "S", "entry_price": 3000, "close_price": 2900,
                "quantity": 0.1, "entry_size_usdt": 300, "realized_pnl_usdt": 10,
                "fee_total_usdt": 0.3, "r_multiple": 0.8, "close_reason": "tp_hit",
                "regime_at_entry": "bear", "opened_at_ms": 2000, "closed_at_ms": 5000,
                "bars_held": 3,
            },
        ]
        views = TradesViewBuilder.build_from_records(records)
        assert len(views) == 2
        assert all(isinstance(v, TradeView) for v in views)
        # Sorted by closed_at_ms descending
        assert views[0].closed_at_ms >= views[1].closed_at_ms

    def test_skip_missing_fields(self):
        records = [
            {"symbol": "BTCUSDT"},  # Missing position_id
            {"position_id": "p1"},  # Missing symbol
            {"position_id": "p1", "symbol": "BTCUSDT"},  # Missing direction
            {
                "position_id": "p1", "symbol": "BTCUSDT", "direction": "long",
                "closed_at_ms": 0,  # Not closed
            },
        ]
        views = TradesViewBuilder.build_from_records(records)
        assert len(views) == 0

    def test_skip_malformed_records(self):
        records = [
            None,
            "not a dict",
            42,
        ]
        # Should not crash
        views = TradesViewBuilder.build_from_records(records)
        assert len(views) == 0

    def test_duration_computed(self):
        records = [{
            "position_id": "p1", "symbol": "BTCUSDT", "direction": "long",
            "strategy_name": "M", "entry_price": 50000, "close_price": 51000,
            "quantity": 0.01, "entry_size_usdt": 500, "realized_pnl_usdt": 10,
            "fee_total_usdt": 0.5, "r_multiple": 1.0, "close_reason": "tp_hit",
            "regime_at_entry": "bull", "opened_at_ms": 1000, "closed_at_ms": 3000,
            "bars_held": 2,
        }]
        views = TradesViewBuilder.build_from_records(records)
        assert views[0].duration_ms == 2000

    def test_summary_empty(self):
        summary = TradesViewBuilder.compute_summary([])
        assert summary["trade_count"] == 0
        assert summary["win_rate"] == 0.0

    def test_summary_with_trades(self):
        records = [
            {
                "position_id": "p1", "symbol": "BTCUSDT", "direction": "long",
                "strategy_name": "M", "entry_price": 50000, "close_price": 51000,
                "quantity": 0.01, "entry_size_usdt": 500, "realized_pnl_usdt": 10,
                "fee_total_usdt": 0.5, "r_multiple": 1.0, "close_reason": "tp_hit",
                "regime_at_entry": "bull", "opened_at_ms": 1000, "closed_at_ms": 2000,
                "bars_held": 2,
            },
            {
                "position_id": "p2", "symbol": "ETHUSDT", "direction": "long",
                "strategy_name": "M", "entry_price": 3000, "close_price": 2800,
                "quantity": 0.1, "entry_size_usdt": 300, "realized_pnl_usdt": -20,
                "fee_total_usdt": 0.3, "r_multiple": -1.0, "close_reason": "sl_hit",
                "regime_at_entry": "bull", "opened_at_ms": 1000, "closed_at_ms": 3000,
                "bars_held": 3,
            },
        ]
        views = TradesViewBuilder.build_from_records(records)
        summary = TradesViewBuilder.compute_summary(views)
        assert summary["trade_count"] == 2
        assert summary["win_count"] == 1
        assert summary["loss_count"] == 1
        assert summary["win_rate"] == pytest.approx(0.5)
        assert summary["by_reason"]["tp_hit"] == 1
        assert summary["by_reason"]["sl_hit"] == 1


# ══════════════════════════════════════════════════════════════
# 2. POSITIONS VIEW BUILDER
# ══════════════════════════════════════════════════════════════

class TestPositionsViewBuilder:
    def test_build_from_records(self):
        records = [{
            "position_id": "p1", "symbol": "BTCUSDT", "direction": "long",
            "strategy_name": "M", "entry_price": 50000, "current_price": 50500,
            "quantity": 0.01, "entry_size_usdt": 500, "current_size_usdt": 505,
            "unrealized_pnl_usdt": 5, "unrealized_pnl_pct": 0.01,
            "stop_loss": 49000, "take_profit": 52000, "regime_at_entry": "bull",
            "opened_at_ms": 1000, "bars_held": 3,
        }]
        views = PositionsViewBuilder.build_from_records(records)
        assert len(views) == 1
        assert isinstance(views[0], PositionView)
        assert views[0].symbol == "BTCUSDT"

    def test_skip_incomplete(self):
        records = [
            {"symbol": "BTCUSDT"},  # No position_id
            {"position_id": "p1"},  # No symbol
        ]
        views = PositionsViewBuilder.build_from_records(records)
        assert len(views) == 0

    def test_exposure_empty(self):
        exp = PositionsViewBuilder.compute_exposure([])
        assert exp["position_count"] == 0
        assert exp["total_exposure_usdt"] == 0.0

    def test_exposure_with_positions(self):
        records = [
            {
                "position_id": "p1", "symbol": "BTCUSDT", "direction": "long",
                "strategy_name": "M", "entry_price": 50000, "current_price": 50500,
                "quantity": 0.01, "entry_size_usdt": 500, "current_size_usdt": 505,
                "unrealized_pnl_usdt": 5, "unrealized_pnl_pct": 0.01,
                "stop_loss": 49000, "take_profit": 52000, "regime_at_entry": "bull",
                "opened_at_ms": 1000, "bars_held": 3,
            },
            {
                "position_id": "p2", "symbol": "ETHUSDT", "direction": "short",
                "strategy_name": "S", "entry_price": 3000, "current_price": 2900,
                "quantity": 0.1, "entry_size_usdt": 300, "current_size_usdt": 290,
                "unrealized_pnl_usdt": 10, "unrealized_pnl_pct": 0.033,
                "stop_loss": 3100, "take_profit": 2800, "regime_at_entry": "bear",
                "opened_at_ms": 2000, "bars_held": 2,
            },
        ]
        views = PositionsViewBuilder.build_from_records(records)
        exp = PositionsViewBuilder.compute_exposure(views)
        assert exp["position_count"] == 2
        assert exp["total_exposure_usdt"] == pytest.approx(795.0)
        assert exp["by_direction"]["long"] == pytest.approx(505.0)
        assert exp["by_direction"]["short"] == pytest.approx(290.0)


# ══════════════════════════════════════════════════════════════
# 3. HEALTH VIEW BUILDER
# ══════════════════════════════════════════════════════════════

class TestHealthViewBuilder:
    def test_all_defaults(self):
        hv = HealthViewBuilder.build()
        assert hv.kill_switch_state == "armed"
        assert hv.circuit_breaker_state == "normal"
        assert hv.failure_mode_tier == "normal"
        assert hv.recovery_complete is True
        assert hv.overall_status == "healthy"

    def test_kill_switch_disarmed(self):
        hv = HealthViewBuilder.build(
            kill_switch_status={"state": "disarmed", "disarm_reason": "manual", "disarmed_at_ms": 1000}
        )
        assert hv.kill_switch_state == "disarmed"
        assert hv.kill_switch_reason == "manual"
        assert hv.overall_status == "halted"

    def test_circuit_breaker_tripped(self):
        hv = HealthViewBuilder.build(
            circuit_breaker_status={"state": "tripped", "tripped_at_ms": 2000}
        )
        assert hv.circuit_breaker_state == "tripped"
        assert hv.overall_status == "circuit_breaker_tripped"

    def test_recovery_pending(self):
        hv = HealthViewBuilder.build(
            recovery_state={
                "recovery_complete": True,
                "trading_allowed": False,
                "last_report": {"reconciliation": {"is_clean": False, "mismatch_count": 3}},
            }
        )
        assert hv.recovery_trading_allowed is False
        assert hv.last_reconciliation_clean is False
        assert hv.last_reconciliation_mismatches == 3
        assert hv.overall_status == "recovery_pending"

    def test_failure_mode_degraded(self):
        hv = HealthViewBuilder.build(
            failure_mode_state={"tier": "degraded", "active_detectors": 2}
        )
        assert hv.failure_mode_tier == "degraded"
        assert hv.failure_mode_detectors == 2

    def test_edge_states_passed_through(self):
        hv = HealthViewBuilder.build(
            edge_states={"MX": "normal", "MPC": "warning"}
        )
        assert hv.edge_states["MPC"] == "warning"


# ══════════════════════════════════════════════════════════════
# 4. METRICS VIEW BUILDER
# ══════════════════════════════════════════════════════════════

class TestMetricsViewBuilder:
    def test_empty_inputs(self):
        mv = MetricsViewBuilder.build()
        assert mv.total_observations == 0
        assert len(mv.slippage_by_symbol) == 0
        assert len(mv.latency_by_symbol) == 0

    def test_slippage_metrics(self):
        mv = MetricsViewBuilder.build(
            quality_stats={
                "BTCUSDT": {
                    "mean": 3.5, "median": 3.0, "p75": 5.0, "stddev": 1.5,
                    "observation_count": 100, "degraded": False,
                },
                "ETHUSDT": {
                    "mean": 8.0, "median": 7.5, "p75": 12.0, "stddev": 4.0,
                    "observation_count": 50, "degraded": True,
                },
            }
        )
        assert len(mv.slippage_by_symbol) == 2
        assert mv.total_observations == 150
        assert mv.degraded_symbols == ["ETHUSDT"]
        assert mv.slippage_by_symbol["BTCUSDT"].mean_slippage_bps == 3.5
        assert mv.slippage_by_symbol["ETHUSDT"].is_degraded is True

    def test_latency_metrics(self):
        mv = MetricsViewBuilder.build(
            latency_data={
                "BTCUSDT": {
                    "ema_ms": 1500, "p50_ms": 1200, "p75_ms": 1800,
                    "p90_ms": 2500, "p99_ms": 4000, "observation_count": 80,
                    "alerts": ["total_pipeline > 8000ms"],
                },
            }
        )
        assert len(mv.latency_by_symbol) == 1
        lm = mv.latency_by_symbol["BTCUSDT"]
        assert lm.ema_total_ms == 1500
        assert lm.p50_ms == 1200
        assert len(lm.alerts) == 1
        assert mv.latency_alerts == ["total_pipeline > 8000ms"]

    def test_to_dict(self):
        mv = MetricsViewBuilder.build(
            quality_stats={"BTCUSDT": {"mean": 3.5, "median": 3.0, "p75": 5.0, "stddev": 1.5, "observation_count": 100}},
        )
        d = mv.to_dict()
        assert "slippage_by_symbol" in d
        assert "BTCUSDT" in d["slippage_by_symbol"]
