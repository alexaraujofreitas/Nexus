"""
Phase 9 Test Suite — Monitoring API

Tests:
- MonitoringDataSource default implementations
- DictDataSource callable wiring
- MonitoringAPI: get_trades, get_positions, get_health, get_metrics,
  get_decisions, get_alerts, get_snapshot
- Isolation proof: no forbidden imports in Phase 9 source
- Replay consistency: same data source → identical snapshots
"""

import pytest
from core.intraday.monitoring.views.monitoring_api import (
    MonitoringAPI,
    MonitoringDataSource,
    DictDataSource,
)
from core.intraday.monitoring.views.alert_manager import AlertManager
from core.intraday.monitoring.views.view_contracts import (
    AlertRecord,
    AlertSeverity,
    DecisionView,
    HealthView,
    MetricsView,
    MonitoringSnapshot,
    PositionView,
    TradeView,
)


# ══════════════════════════════════════════════════════════════
# FIXTURES
# ══════════════════════════════════════════════════════════════

def _make_trade_record(**overrides):
    base = {
        "position_id": "p1", "symbol": "BTCUSDT", "direction": "long",
        "strategy_name": "MomentumBreakout", "entry_price": 50000,
        "close_price": 51000, "quantity": 0.01, "entry_size_usdt": 500,
        "realized_pnl_usdt": 10, "fee_total_usdt": 0.5, "r_multiple": 1.0,
        "close_reason": "tp_hit", "regime_at_entry": "bull",
        "opened_at_ms": 1000, "closed_at_ms": 3000, "bars_held": 2,
    }
    base.update(overrides)
    return base


def _make_position_record(**overrides):
    base = {
        "position_id": "p1", "symbol": "BTCUSDT", "direction": "long",
        "strategy_name": "MomentumBreakout", "entry_price": 50000,
        "current_price": 50500, "quantity": 0.01, "entry_size_usdt": 500,
        "current_size_usdt": 505, "unrealized_pnl_usdt": 5,
        "unrealized_pnl_pct": 0.01, "stop_loss": 49000, "take_profit": 52000,
        "regime_at_entry": "bull", "opened_at_ms": 1000, "bars_held": 3,
    }
    base.update(overrides)
    return base


def _make_decision_record(**overrides):
    base = {
        "decision_id": "d1", "intent_id": "i1", "symbol": "BTCUSDT",
        "direction": "long", "strategy_name": "MomentumBreakout",
        "status": "approved", "rejection_reason": "", "rejection_source": "",
        "risk_scaling_applied": 1.0, "final_size_usdt": 500,
        "created_at_ms": 1000,
    }
    base.update(overrides)
    return base


# ══════════════════════════════════════════════════════════════
# 1. DATA SOURCE DEFAULTS
# ══════════════════════════════════════════════════════════════

class TestMonitoringDataSourceDefaults:
    def test_default_returns_empty(self):
        src = MonitoringDataSource()
        assert src.get_open_positions() == []
        assert src.get_closed_trades() == []
        assert src.get_recent_decisions() == []
        assert src.get_edge_states() == {}
        assert src.get_quality_stats() == {}
        assert src.get_latency_data() == {}

    def test_default_kill_switch_armed(self):
        src = MonitoringDataSource()
        assert src.get_kill_switch_status()["state"] == "armed"

    def test_default_circuit_breaker_normal(self):
        src = MonitoringDataSource()
        assert src.get_circuit_breaker_status()["state"] == "normal"

    def test_default_recovery_complete(self):
        src = MonitoringDataSource()
        state = src.get_recovery_state()
        assert state["recovery_complete"] is True
        assert state["trading_allowed"] is True


# ══════════════════════════════════════════════════════════════
# 2. DICT DATA SOURCE
# ══════════════════════════════════════════════════════════════

class TestDictDataSource:
    def test_callable_wiring(self):
        positions = [_make_position_record()]
        src = DictDataSource(open_positions_fn=lambda: positions)
        assert len(src.get_open_positions()) == 1

    def test_missing_fn_returns_default(self):
        src = DictDataSource()  # No fns registered
        assert src.get_open_positions() == []
        assert src.get_kill_switch_status()["state"] == "armed"
        assert src.get_circuit_breaker_status()["state"] == "normal"

    def test_fn_exception_returns_default(self):
        def bad_fn():
            raise RuntimeError("boom")

        src = DictDataSource(open_positions_fn=bad_fn)
        assert src.get_open_positions() == []

    def test_closed_trades_with_limit(self):
        def trades_fn(limit):
            return [_make_trade_record(position_id=f"p{i}") for i in range(limit)]

        src = DictDataSource(closed_trades_fn=trades_fn)
        assert len(src.get_closed_trades(3)) == 3

    def test_closed_trades_no_limit_fallback(self):
        """If the callable doesn't accept limit, still works."""
        def trades_fn():
            return [_make_trade_record()]

        src = DictDataSource(closed_trades_fn=trades_fn)
        result = src.get_closed_trades(10)
        assert len(result) == 1

    def test_capital_snapshot(self):
        src = DictDataSource(
            capital_snapshot_fn=lambda: {"total_capital": 10000, "available_capital": 8000}
        )
        snap = src.get_capital_snapshot()
        assert snap["total_capital"] == 10000


# ══════════════════════════════════════════════════════════════
# 3. MONITORING API — TRADES
# ══════════════════════════════════════════════════════════════

class TestMonitoringAPITrades:
    def test_get_trades(self):
        records = [_make_trade_record(), _make_trade_record(position_id="p2", closed_at_ms=5000)]
        src = DictDataSource(closed_trades_fn=lambda limit: records[:limit])
        api = MonitoringAPI(src, now_ms_fn=lambda: 10000)

        trades = api.get_trades(limit=10)
        assert len(trades) == 2
        assert all(isinstance(t, TradeView) for t in trades)

    def test_get_trades_empty(self):
        src = DictDataSource()
        api = MonitoringAPI(src, now_ms_fn=lambda: 10000)
        assert api.get_trades() == []

    def test_get_trade_summary(self):
        records = [
            _make_trade_record(realized_pnl_usdt=10),
            _make_trade_record(position_id="p2", realized_pnl_usdt=-5,
                                close_reason="sl_hit", closed_at_ms=5000),
        ]
        src = DictDataSource(closed_trades_fn=lambda limit: records[:limit])
        api = MonitoringAPI(src, now_ms_fn=lambda: 10000)

        summary = api.get_trade_summary()
        assert summary["trade_count"] == 2
        assert summary["win_count"] == 1
        assert summary["loss_count"] == 1


# ══════════════════════════════════════════════════════════════
# 4. MONITORING API — POSITIONS
# ══════════════════════════════════════════════════════════════

class TestMonitoringAPIPositions:
    def test_get_positions(self):
        records = [_make_position_record()]
        src = DictDataSource(open_positions_fn=lambda: records)
        api = MonitoringAPI(src, now_ms_fn=lambda: 10000)

        positions = api.get_positions()
        assert len(positions) == 1
        assert isinstance(positions[0], PositionView)

    def test_get_exposure(self):
        records = [
            _make_position_record(current_size_usdt=500),
            _make_position_record(position_id="p2", symbol="ETHUSDT",
                                   direction="short", current_size_usdt=300),
        ]
        src = DictDataSource(open_positions_fn=lambda: records)
        api = MonitoringAPI(src, now_ms_fn=lambda: 10000)

        exp = api.get_exposure()
        assert exp["position_count"] == 2
        assert exp["total_exposure_usdt"] == pytest.approx(800.0)

    def test_get_positions_empty(self):
        src = DictDataSource()
        api = MonitoringAPI(src, now_ms_fn=lambda: 10000)
        assert api.get_positions() == []


# ══════════════════════════════════════════════════════════════
# 5. MONITORING API — HEALTH
# ══════════════════════════════════════════════════════════════

class TestMonitoringAPIHealth:
    def test_healthy_defaults(self):
        src = DictDataSource()
        api = MonitoringAPI(src, now_ms_fn=lambda: 10000)
        health = api.get_health()
        assert isinstance(health, HealthView)
        assert health.overall_status == "healthy"

    def test_kill_switch_disarmed(self):
        src = DictDataSource(
            kill_switch_fn=lambda: {"state": "disarmed", "disarm_reason": "drawdown"}
        )
        api = MonitoringAPI(src, now_ms_fn=lambda: 10000)
        health = api.get_health()
        assert health.overall_status == "halted"
        assert health.kill_switch_state == "disarmed"

    def test_circuit_breaker_tripped(self):
        src = DictDataSource(
            circuit_breaker_fn=lambda: {"state": "tripped"}
        )
        api = MonitoringAPI(src, now_ms_fn=lambda: 10000)
        health = api.get_health()
        assert health.overall_status == "circuit_breaker_tripped"


# ══════════════════════════════════════════════════════════════
# 6. MONITORING API — METRICS
# ══════════════════════════════════════════════════════════════

class TestMonitoringAPIMetrics:
    def test_get_metrics_empty(self):
        src = DictDataSource()
        api = MonitoringAPI(src, now_ms_fn=lambda: 10000)
        metrics = api.get_metrics()
        assert isinstance(metrics, MetricsView)
        assert metrics.total_observations == 0

    def test_get_metrics_with_data(self):
        src = DictDataSource(
            quality_stats_fn=lambda: {
                "BTCUSDT": {"mean": 3.5, "median": 3.0, "p75": 5.0,
                             "stddev": 1.5, "observation_count": 100},
            },
            latency_data_fn=lambda: {
                "BTCUSDT": {"ema_ms": 1500, "p50_ms": 1200, "p75_ms": 1800,
                             "p90_ms": 2500, "p99_ms": 4000, "observation_count": 80},
            },
        )
        api = MonitoringAPI(src, now_ms_fn=lambda: 10000)
        metrics = api.get_metrics()
        assert metrics.total_observations == 100
        assert "BTCUSDT" in metrics.slippage_by_symbol
        assert "BTCUSDT" in metrics.latency_by_symbol


# ══════════════════════════════════════════════════════════════
# 7. MONITORING API — DECISIONS
# ══════════════════════════════════════════════════════════════

class TestMonitoringAPIDecisions:
    def test_get_decisions(self):
        records = [
            _make_decision_record(),
            _make_decision_record(decision_id="d2", status="rejected",
                                   rejection_reason="portfolio_heat"),
        ]
        src = DictDataSource(recent_decisions_fn=lambda limit: records[:limit])
        api = MonitoringAPI(src, now_ms_fn=lambda: 10000)

        decisions = api.get_decisions()
        assert len(decisions) == 2
        assert all(isinstance(d, DecisionView) for d in decisions)
        assert decisions[0].status == "approved"
        assert decisions[1].status == "rejected"

    def test_get_decisions_skips_malformed(self):
        records = [
            _make_decision_record(),
            None,  # Malformed
            "bad",  # Malformed
        ]
        src = DictDataSource(recent_decisions_fn=lambda limit: records[:limit])
        api = MonitoringAPI(src, now_ms_fn=lambda: 10000)

        decisions = api.get_decisions()
        assert len(decisions) == 1

    def test_get_decisions_empty(self):
        src = DictDataSource()
        api = MonitoringAPI(src, now_ms_fn=lambda: 10000)
        assert api.get_decisions() == []


# ══════════════════════════════════════════════════════════════
# 8. MONITORING API — ALERTS
# ══════════════════════════════════════════════════════════════

class TestMonitoringAPIAlerts:
    def test_get_alerts_via_api(self):
        alert_mgr = AlertManager(now_ms_fn=lambda: 1000)
        alert_mgr.raise_alert(
            severity=AlertSeverity.WARNING, reason_code="test",
            message="msg", source="src",
        )
        src = DictDataSource()
        api = MonitoringAPI(src, alert_manager=alert_mgr, now_ms_fn=lambda: 10000)

        alerts = api.get_alerts()
        assert len(alerts) == 1
        assert alerts[0].reason_code == "test"

    def test_alert_manager_accessible(self):
        src = DictDataSource()
        api = MonitoringAPI(src, now_ms_fn=lambda: 10000)
        assert api.alert_manager is not None


# ══════════════════════════════════════════════════════════════
# 9. MONITORING API — SNAPSHOT
# ══════════════════════════════════════════════════════════════

class TestMonitoringAPISnapshot:
    def test_get_snapshot_complete(self):
        trade_records = [_make_trade_record()]
        position_records = [_make_position_record(current_size_usdt=505, unrealized_pnl_usdt=5)]
        decision_records = [_make_decision_record()]

        src = DictDataSource(
            closed_trades_fn=lambda limit: trade_records[:limit],
            open_positions_fn=lambda: position_records,
            recent_decisions_fn=lambda limit: decision_records[:limit],
            capital_snapshot_fn=lambda: {"total_capital": 10000, "available_capital": 8000},
        )
        api = MonitoringAPI(src, now_ms_fn=lambda: 50000)

        snap = api.get_snapshot()
        assert isinstance(snap, MonitoringSnapshot)
        assert snap.timestamp_ms == 50000
        assert snap.total_open_positions == 1
        assert snap.total_unrealized_pnl == pytest.approx(5.0)
        assert snap.total_exposure_usdt == pytest.approx(505.0)
        assert snap.total_capital_usdt == pytest.approx(10000.0)
        assert snap.available_capital_usdt == pytest.approx(8000.0)

        # Sub-views populated
        assert isinstance(snap.health, HealthView)
        assert snap.health.overall_status == "healthy"
        assert len(snap.open_positions) == 1
        assert len(snap.recent_trades) == 1
        assert len(snap.recent_decisions) == 1

    def test_snapshot_empty_data(self):
        src = DictDataSource()
        api = MonitoringAPI(src, now_ms_fn=lambda: 1000)
        snap = api.get_snapshot()
        assert snap.total_open_positions == 0
        assert snap.total_unrealized_pnl == 0.0
        assert snap.total_exposure_usdt == 0.0

    def test_snapshot_frozen(self):
        src = DictDataSource()
        api = MonitoringAPI(src, now_ms_fn=lambda: 1000)
        snap = api.get_snapshot()
        with pytest.raises(AttributeError):
            snap.timestamp_ms = 9999

    def test_snapshot_to_dict(self):
        src = DictDataSource(
            capital_snapshot_fn=lambda: {"total_capital": 10000, "available_capital": 8000}
        )
        api = MonitoringAPI(src, now_ms_fn=lambda: 5000)
        snap = api.get_snapshot()
        d = snap.to_dict()
        assert d["timestamp_ms"] == 5000
        assert "health" in d
        assert d["health"]["overall_status"] == "healthy"


# ══════════════════════════════════════════════════════════════
# 10. DIAGNOSTICS
# ══════════════════════════════════════════════════════════════

class TestMonitoringAPIDiagnostics:
    def test_get_state(self):
        src = DictDataSource()
        api = MonitoringAPI(src, now_ms_fn=lambda: 1000)
        state = api.get_state()
        assert "alert_state" in state
        assert "data_source_type" in state
        assert state["data_source_type"] == "DictDataSource"


# ══════════════════════════════════════════════════════════════
# 11. REPLAY CONSISTENCY
# ══════════════════════════════════════════════════════════════

class TestReplayConsistency:
    def test_same_source_same_snapshot(self):
        """Identical data sources produce identical snapshots."""
        def make_api():
            records = [_make_position_record()]
            src = DictDataSource(
                open_positions_fn=lambda: records,
                capital_snapshot_fn=lambda: {"total_capital": 10000, "available_capital": 8000},
            )
            return MonitoringAPI(src, now_ms_fn=lambda: 5000)

        snap1 = make_api().get_snapshot().to_dict()
        snap2 = make_api().get_snapshot().to_dict()

        assert snap1["timestamp_ms"] == snap2["timestamp_ms"]
        assert snap1["total_open_positions"] == snap2["total_open_positions"]
        assert snap1["total_unrealized_pnl"] == snap2["total_unrealized_pnl"]
        assert snap1["health"] == snap2["health"]


# ══════════════════════════════════════════════════════════════
# 12. ISOLATION PROOF — NO FORBIDDEN IMPORTS
# ══════════════════════════════════════════════════════════════

class TestIsolationProof:
    """
    Phase 9 source files must NEVER import from execution engine,
    portfolio state, PySide6, or any mutable upstream component.
    """

    PHASE9_SOURCE_DIR = "core/intraday/monitoring/views"
    FORBIDDEN_PATTERNS = [
        "from PySide6",
        "import PySide6",
        "from core.intraday.execution",
        "from core.intraday.live",
        "from core.risk",
        "from core.meta_decision",
        "from core.portfolio",
        "import portfolio",
    ]

    def test_no_forbidden_imports(self):
        import os
        import re

        source_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            self.PHASE9_SOURCE_DIR,
        )

        violations = []
        for fname in os.listdir(source_dir):
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(source_dir, fname)
            with open(fpath, "r") as f:
                content = f.read()
            for pattern in self.FORBIDDEN_PATTERNS:
                if pattern in content:
                    violations.append(f"{fname}: contains '{pattern}'")

        assert violations == [], f"Forbidden imports found:\n" + "\n".join(violations)

    def test_no_mutation_methods(self):
        """Phase 9 view contracts should not have setter methods or mutating APIs."""
        import os

        source_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            self.PHASE9_SOURCE_DIR,
        )

        violations = []
        for fname in os.listdir(source_dir):
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(source_dir, fname)
            with open(fpath, "r") as f:
                lines = f.readlines()
            for i, line in enumerate(lines, 1):
                stripped = line.strip()
                # Check for methods that could mutate upstream state
                if any(kw in stripped for kw in [
                    "def submit_order", "def execute", "def place_order",
                    "def cancel_order", "def modify_order", "def close_position",
                    "def open_position", "def set_capital",
                ]):
                    violations.append(f"{fname}:{i}: {stripped}")

        assert violations == [], f"Mutation methods found:\n" + "\n".join(violations)
