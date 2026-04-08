"""
Phase 9 Test Suite — View Contracts & Immutability

Tests:
- All view dataclasses are frozen (immutable)
- to_dict() produces complete, JSON-safe output
- Computed properties work correctly
- AlertSeverity enum completeness
- MonitoringSnapshot aggregation
"""

import pytest
from core.intraday.monitoring.views.view_contracts import (
    AlertRecord,
    AlertSeverity,
    DecisionView,
    HealthView,
    LatencyMetrics,
    MetricsView,
    MonitoringSnapshot,
    PositionView,
    SlippageMetrics,
    TradeView,
)


# ══════════════════════════════════════════════════════════════
# 1. IMMUTABILITY
# ══════════════════════════════════════════════════════════════

class TestImmutability:
    """All view contracts must be frozen (immutable)."""

    def test_trade_view_frozen(self):
        tv = TradeView(
            position_id="p1", symbol="BTCUSDT", direction="long",
            strategy_name="MomentumBreakout", entry_price=50000, exit_price=51000,
            quantity=0.01, entry_size_usdt=500, realized_pnl_usdt=10,
            fee_total_usdt=0.5, r_multiple=1.0, close_reason="tp_hit",
            regime_at_entry="bull_trend", opened_at_ms=1000, closed_at_ms=2000,
            duration_ms=1000, bars_held=2,
        )
        with pytest.raises(AttributeError):
            tv.symbol = "ETHUSDT"

    def test_position_view_frozen(self):
        pv = PositionView(
            position_id="p1", symbol="BTCUSDT", direction="long",
            strategy_name="MomentumBreakout", entry_price=50000, current_price=50500,
            quantity=0.01, entry_size_usdt=500, current_size_usdt=505,
            unrealized_pnl_usdt=5, unrealized_pnl_pct=0.01,
            stop_loss=49000, take_profit=52000, regime_at_entry="bull_trend",
            opened_at_ms=1000, bars_held=3,
        )
        with pytest.raises(AttributeError):
            pv.current_price = 51000

    def test_health_view_frozen(self):
        hv = HealthView(kill_switch_state="armed")
        with pytest.raises(AttributeError):
            hv.kill_switch_state = "disarmed"

    def test_alert_record_frozen(self):
        ar = AlertRecord(
            alert_id="a1", timestamp_ms=1000, severity=AlertSeverity.WARNING,
            reason_code="test", message="test msg", source="test",
        )
        with pytest.raises(AttributeError):
            ar.severity = AlertSeverity.CRITICAL

    def test_slippage_metrics_frozen(self):
        sm = SlippageMetrics(
            symbol="BTCUSDT", mean_slippage_bps=3.5, median_slippage_bps=3.0,
            p75_slippage_bps=5.0, stddev_slippage_bps=1.5,
            observation_count=100, is_degraded=False,
        )
        with pytest.raises(AttributeError):
            sm.is_degraded = True

    def test_latency_metrics_frozen(self):
        lm = LatencyMetrics(symbol="BTCUSDT", ema_total_ms=1500)
        with pytest.raises(AttributeError):
            lm.ema_total_ms = 2000

    def test_decision_view_frozen(self):
        dv = DecisionView(
            decision_id="d1", intent_id="i1", symbol="BTCUSDT",
            direction="long", strategy_name="MomentumBreakout", status="approved",
        )
        with pytest.raises(AttributeError):
            dv.status = "rejected"


# ══════════════════════════════════════════════════════════════
# 2. TRADE VIEW
# ══════════════════════════════════════════════════════════════

class TestTradeView:
    def test_is_winner_positive_pnl(self):
        tv = TradeView(
            position_id="p1", symbol="BTCUSDT", direction="long",
            strategy_name="M", entry_price=50000, exit_price=51000,
            quantity=0.01, entry_size_usdt=500, realized_pnl_usdt=10,
            fee_total_usdt=0.5, r_multiple=1.0, close_reason="tp_hit",
            regime_at_entry="bull", opened_at_ms=1000, closed_at_ms=2000,
            duration_ms=1000, bars_held=2,
        )
        assert tv.is_winner is True
        assert tv.net_pnl_usdt == pytest.approx(9.5)

    def test_is_loser_negative_pnl(self):
        tv = TradeView(
            position_id="p1", symbol="BTCUSDT", direction="long",
            strategy_name="M", entry_price=50000, exit_price=49000,
            quantity=0.01, entry_size_usdt=500, realized_pnl_usdt=-10,
            fee_total_usdt=0.5, r_multiple=-1.0, close_reason="sl_hit",
            regime_at_entry="bull", opened_at_ms=1000, closed_at_ms=2000,
            duration_ms=1000, bars_held=2,
        )
        assert tv.is_winner is False
        assert tv.net_pnl_usdt == pytest.approx(-10.5)

    def test_to_dict_completeness(self):
        tv = TradeView(
            position_id="p1", symbol="BTCUSDT", direction="long",
            strategy_name="M", entry_price=50000, exit_price=51000,
            quantity=0.01, entry_size_usdt=500, realized_pnl_usdt=10,
            fee_total_usdt=0.5, r_multiple=1.0, close_reason="tp_hit",
            regime_at_entry="bull", opened_at_ms=1000, closed_at_ms=2000,
            duration_ms=1000, bars_held=2,
        )
        d = tv.to_dict()
        assert d["position_id"] == "p1"
        assert d["realized_pnl_usdt"] == 10
        assert "slippage_pct" in d


# ══════════════════════════════════════════════════════════════
# 3. POSITION VIEW
# ══════════════════════════════════════════════════════════════

class TestPositionView:
    def test_risk_usdt(self):
        pv = PositionView(
            position_id="p1", symbol="BTCUSDT", direction="long",
            strategy_name="M", entry_price=50000, current_price=50500,
            quantity=0.01, entry_size_usdt=500, current_size_usdt=505,
            unrealized_pnl_usdt=5, unrealized_pnl_pct=0.01,
            stop_loss=49000, take_profit=52000, regime_at_entry="bull",
            opened_at_ms=1000, bars_held=3,
        )
        # risk_per_unit = |50000 - 49000| = 1000
        # risk_usdt = 1000 * 0.01 = 10
        assert pv.risk_usdt == pytest.approx(10.0)

    def test_current_r_long(self):
        pv = PositionView(
            position_id="p1", symbol="BTCUSDT", direction="long",
            strategy_name="M", entry_price=50000, current_price=51000,
            quantity=0.01, entry_size_usdt=500, current_size_usdt=510,
            unrealized_pnl_usdt=10, unrealized_pnl_pct=0.02,
            stop_loss=49000, take_profit=52000, regime_at_entry="bull",
            opened_at_ms=1000, bars_held=3,
        )
        # move = 51000 - 50000 = 1000, risk_per_unit = 1000
        assert pv.current_r == pytest.approx(1.0)

    def test_current_r_short(self):
        pv = PositionView(
            position_id="p1", symbol="BTCUSDT", direction="short",
            strategy_name="M", entry_price=50000, current_price=49000,
            quantity=0.01, entry_size_usdt=500, current_size_usdt=490,
            unrealized_pnl_usdt=10, unrealized_pnl_pct=0.02,
            stop_loss=51000, take_profit=48000, regime_at_entry="bear",
            opened_at_ms=1000, bars_held=3,
        )
        # move = 50000 - 49000 = 1000, risk_per_unit = 1000
        assert pv.current_r == pytest.approx(1.0)

    def test_to_dict_includes_computed(self):
        pv = PositionView(
            position_id="p1", symbol="BTCUSDT", direction="long",
            strategy_name="M", entry_price=50000, current_price=50500,
            quantity=0.01, entry_size_usdt=500, current_size_usdt=505,
            unrealized_pnl_usdt=5, unrealized_pnl_pct=0.01,
            stop_loss=49000, take_profit=52000, regime_at_entry="bull",
            opened_at_ms=1000, bars_held=3,
        )
        d = pv.to_dict()
        assert "risk_usdt" in d
        assert "current_r" in d


# ══════════════════════════════════════════════════════════════
# 4. HEALTH VIEW
# ══════════════════════════════════════════════════════════════

class TestHealthView:
    def test_overall_healthy(self):
        hv = HealthView(kill_switch_state="armed")
        assert hv.overall_status == "healthy"

    def test_overall_halted(self):
        hv = HealthView(kill_switch_state="disarmed")
        assert hv.overall_status == "halted"

    def test_overall_tripped(self):
        hv = HealthView(kill_switch_state="armed", circuit_breaker_state="tripped")
        assert hv.overall_status == "circuit_breaker_tripped"

    def test_overall_recovery_pending(self):
        hv = HealthView(kill_switch_state="armed", recovery_trading_allowed=False)
        assert hv.overall_status == "recovery_pending"

    def test_overall_degraded(self):
        hv = HealthView(kill_switch_state="armed", failure_mode_tier="degraded")
        assert hv.overall_status == "degraded"

    def test_overall_warning(self):
        hv = HealthView(kill_switch_state="armed", circuit_breaker_state="warning")
        assert hv.overall_status == "warning"

    def test_to_dict(self):
        hv = HealthView(
            kill_switch_state="armed",
            circuit_breaker_state="normal",
            edge_states={"MX": "normal", "MPC": "warning"},
        )
        d = hv.to_dict()
        assert d["overall_status"] == "healthy"
        assert d["edge_states"]["MPC"] == "warning"


# ══════════════════════════════════════════════════════════════
# 5. ALERT
# ══════════════════════════════════════════════════════════════

class TestAlertSeverity:
    def test_all_severities(self):
        assert len(AlertSeverity) == 4
        assert AlertSeverity.INFO.value == "info"
        assert AlertSeverity.EMERGENCY.value == "emergency"

    def test_alert_record_to_dict(self):
        ar = AlertRecord(
            alert_id="a1", timestamp_ms=1000, severity=AlertSeverity.CRITICAL,
            reason_code="test", message="msg", source="src",
            symbol="BTCUSDT", metadata='{"key": 1}',
        )
        d = ar.to_dict()
        assert d["severity"] == "critical"
        assert d["symbol"] == "BTCUSDT"


# ══════════════════════════════════════════════════════════════
# 6. MONITORING SNAPSHOT
# ══════════════════════════════════════════════════════════════

class TestMonitoringSnapshot:
    def test_snapshot_to_dict(self):
        hv = HealthView(kill_switch_state="armed")
        snap = MonitoringSnapshot(
            timestamp_ms=1000,
            health=hv,
            total_open_positions=2,
            total_unrealized_pnl=15.5,
            total_capital_usdt=10000.0,
        )
        d = snap.to_dict()
        assert d["timestamp_ms"] == 1000
        assert d["health"]["overall_status"] == "healthy"
        assert d["total_open_positions"] == 2
        assert d["total_unrealized_pnl"] == 15.5

    def test_snapshot_frozen(self):
        hv = HealthView(kill_switch_state="armed")
        snap = MonitoringSnapshot(timestamp_ms=1000, health=hv)
        with pytest.raises(AttributeError):
            snap.timestamp_ms = 2000
