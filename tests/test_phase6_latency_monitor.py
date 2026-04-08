# ============================================================
# Phase 6 Latency Monitor Test Suite (v3 API)
# ============================================================
import pytest
import time
from collections import deque
from core.intraday.monitoring.latency_monitor import (
    LatencyMonitor,
    LatencyConfig,
    LatencyRecord,
    LatencyAlert,
    PipelineStage,
)


# ══════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ══════════════════════════════════════════════════════════════

def _record_full_pipeline(
    monitor: LatencyMonitor,
    trigger_id: str,
    symbol: str,
    strategy: str,
    base_time_ms: int,
    inter_stage_latencies: dict = None,
) -> LatencyRecord:
    """
    Helper: Record all 7 pipeline stages in order.

    Args:
        monitor: LatencyMonitor instance
        trigger_id: Unique ID for this pipeline traversal
        symbol: Trading symbol
        strategy: Strategy class name
        base_time_ms: Starting timestamp (SIGNAL_CREATED)
        inter_stage_latencies: Optional dict mapping "stage1→stage2" to ms.
                               If not provided, uses defaults.

    Returns:
        The completed LatencyRecord
    """
    if inter_stage_latencies is None:
        inter_stage_latencies = {
            "signal_created→processing_start": 10,
            "processing_start→processing_end": 100,
            "processing_end→risk_decision": 50,
            "risk_decision→execution_request": 20,
            "execution_request→order_submitted": 15,
            "order_submitted→fill_received": 200,
        }

    times = {
        PipelineStage.SIGNAL_CREATED: base_time_ms,
    }

    # Accumulate inter-stage latencies
    current_time = base_time_ms
    stage_pairs = [
        (PipelineStage.SIGNAL_CREATED, PipelineStage.PROCESSING_START),
        (PipelineStage.PROCESSING_START, PipelineStage.PROCESSING_END),
        (PipelineStage.PROCESSING_END, PipelineStage.RISK_DECISION),
        (PipelineStage.RISK_DECISION, PipelineStage.EXECUTION_REQUEST),
        (PipelineStage.EXECUTION_REQUEST, PipelineStage.ORDER_SUBMITTED),
        (PipelineStage.ORDER_SUBMITTED, PipelineStage.FILL_RECEIVED),
    ]

    for stage1, stage2 in stage_pairs:
        pair_key = f"{stage1.value}→{stage2.value}"
        latency = inter_stage_latencies.get(pair_key, 50)
        current_time += latency
        times[stage2] = current_time

    # Record each stage
    for stage, ts in times.items():
        monitor.record_stage(trigger_id, symbol, strategy, stage, ts)

    # Retrieve and return the completed record
    record = monitor.get_record(trigger_id)
    return record


# ══════════════════════════════════════════════════════════════
# SECTION 1: Modeling - Latency Estimate (6 tests)
# ══════════════════════════════════════════════════════════════

class TestModelingLatencyEstimate:
    """Tests for get_latency_estimate() deterministic modeling API."""

    def test_cold_start_returns_default_for_unknown_symbol(self):
        """Cold-start: returns default_latency_ms when no data for symbol."""
        monitor = LatencyMonitor(LatencyConfig(default_latency_ms=2000))
        est = monitor.get_latency_estimate("BTCUSDT")
        assert est == 2000

    def test_cold_start_below_min_observations(self):
        """Cold-start: returns default when below min_latency_observations."""
        config = LatencyConfig(
            default_latency_ms=3000,
            min_latency_observations=5,
        )
        monitor = LatencyMonitor(config)
        base_time = int(time.time() * 1000)

        # Record 4 pipelines (less than min_latency_observations=5)
        for i in range(4):
            _record_full_pipeline(monitor, f"tid_{i}", "BTCUSDT", "Strategy", base_time + i * 1000)

        est = monitor.get_latency_estimate("BTCUSDT")
        assert est == 3000

    def test_after_min_observations_returns_ema_estimate(self):
        """After enough observations: returns EMA-based estimate."""
        config = LatencyConfig(
            default_latency_ms=2000,
            min_latency_observations=3,
            latency_ema_alpha=0.2,
        )
        monitor = LatencyMonitor(config)
        base_time = int(time.time() * 1000)

        # Record 3 pipelines with known latencies
        latencies_ms = {
            "signal_created→processing_start": 10,
            "processing_start→processing_end": 100,
            "processing_end→risk_decision": 50,
            "risk_decision→execution_request": 20,
            "execution_request→order_submitted": 15,
            "order_submitted→fill_received": 200,  # Total = 395ms
        }

        for i in range(3):
            _record_full_pipeline(
                monitor, f"tid_{i}", "BTCUSDT", "Strategy",
                base_time + i * 1000, latencies_ms
            )

        est = monitor.get_latency_estimate("BTCUSDT")
        # Should not be default, should be EMA-based
        assert est != 2000
        assert est > 0

    def test_estimate_bounded_min_latency(self):
        """Estimate bounded: never below min_latency_ms."""
        config = LatencyConfig(
            default_latency_ms=2000,
            min_latency_observations=2,
            min_latency_ms=100,
            latency_ema_alpha=0.2,
        )
        monitor = LatencyMonitor(config)
        base_time = int(time.time() * 1000)

        # Record pipelines with very small latencies
        small_latencies = {
            "signal_created→processing_start": 1,
            "processing_start→processing_end": 1,
            "processing_end→risk_decision": 1,
            "risk_decision→execution_request": 1,
            "execution_request→order_submitted": 1,
            "order_submitted→fill_received": 1,  # Total = 6ms
        }

        for i in range(3):
            _record_full_pipeline(
                monitor, f"tid_{i}", "ETHUSDT", "Strategy",
                base_time + i * 1000, small_latencies
            )

        est = monitor.get_latency_estimate("ETHUSDT")
        assert est >= 100  # Must respect min_latency_ms

    def test_estimate_bounded_max_latency(self):
        """Estimate bounded: never above max_latency_ms."""
        config = LatencyConfig(
            default_latency_ms=2000,
            min_latency_observations=2,
            max_latency_ms=10000,
            latency_ema_alpha=0.2,
        )
        monitor = LatencyMonitor(config)
        base_time = int(time.time() * 1000)

        # Record pipelines with very large latencies
        large_latencies = {
            "signal_created→processing_start": 10000,
            "processing_start→processing_end": 10000,
            "processing_end→risk_decision": 10000,
            "risk_decision→execution_request": 10000,
            "execution_request→order_submitted": 10000,
            "order_submitted→fill_received": 10000,  # Total = 60000ms
        }

        for i in range(3):
            _record_full_pipeline(
                monitor, f"tid_{i}", "SOLUSDT", "Strategy",
                base_time + i * 1000, large_latencies
            )

        est = monitor.get_latency_estimate("SOLUSDT")
        assert est <= 10000  # Must respect max_latency_ms

    def test_different_symbols_get_independent_estimates(self):
        """Different symbols get independent estimates."""
        config = LatencyConfig(
            default_latency_ms=2000,
            min_latency_observations=2,
            latency_ema_alpha=0.2,
        )
        monitor = LatencyMonitor(config)
        base_time = int(time.time() * 1000)

        # Record for BTCUSDT with fast latencies
        fast_latencies = {
            "signal_created→processing_start": 5,
            "processing_start→processing_end": 50,
            "processing_end→risk_decision": 25,
            "risk_decision→execution_request": 10,
            "execution_request→order_submitted": 8,
            "order_submitted→fill_received": 100,  # Total = 198ms
        }

        # Record for ETHUSDT with slow latencies
        slow_latencies = {
            "signal_created→processing_start": 100,
            "processing_start→processing_end": 500,
            "processing_end→risk_decision": 250,
            "risk_decision→execution_request": 100,
            "execution_request→order_submitted": 80,
            "order_submitted→fill_received": 1000,  # Total = 2030ms
        }

        for i in range(2):
            _record_full_pipeline(
                monitor, f"btc_{i}", "BTCUSDT", "Strategy",
                base_time + i * 2000, fast_latencies
            )
            _record_full_pipeline(
                monitor, f"eth_{i}", "ETHUSDT", "Strategy",
                base_time + i * 2000, slow_latencies
            )

        est_btc = monitor.get_latency_estimate("BTCUSDT")
        est_eth = monitor.get_latency_estimate("ETHUSDT")

        # They should be significantly different
        assert est_btc < est_eth


# ══════════════════════════════════════════════════════════════
# SECTION 2: Modeling - Determinism (2 tests)
# ══════════════════════════════════════════════════════════════

class TestModelingDeterminism:
    """Tests for deterministic behavior of latency estimates."""

    def test_same_observation_sequence_returns_same_estimate(self):
        """Same observation sequence → same estimate."""
        config = LatencyConfig(
            default_latency_ms=2000,
            min_latency_observations=3,
            latency_ema_alpha=0.2,
        )

        latencies = {
            "signal_created→processing_start": 10,
            "processing_start→processing_end": 100,
            "processing_end→risk_decision": 50,
            "risk_decision→execution_request": 20,
            "execution_request→order_submitted": 15,
            "order_submitted→fill_received": 200,
        }

        # First monitor
        monitor1 = LatencyMonitor(config)
        base_time = 1000000
        for i in range(3):
            _record_full_pipeline(
                monitor1, f"tid_{i}", "BTCUSDT", "Strategy",
                base_time + i * 1000, latencies
            )
        est1 = monitor1.get_latency_estimate("BTCUSDT")

        # Second monitor with identical data
        monitor2 = LatencyMonitor(config)
        for i in range(3):
            _record_full_pipeline(
                monitor2, f"tid_{i}", "BTCUSDT", "Strategy",
                base_time + i * 1000, latencies
            )
        est2 = monitor2.get_latency_estimate("BTCUSDT")

        assert est1 == est2

    def test_two_monitors_same_data_same_estimate(self):
        """Two monitors with same data → same estimate."""
        config = LatencyConfig(
            default_latency_ms=2000,
            min_latency_observations=2,
            latency_ema_alpha=0.3,
        )

        latencies = {
            "signal_created→processing_start": 20,
            "processing_start→processing_end": 150,
            "processing_end→risk_decision": 80,
            "risk_decision→execution_request": 30,
            "execution_request→order_submitted": 25,
            "order_submitted→fill_received": 250,
        }

        monitors = [LatencyMonitor(config) for _ in range(2)]
        base_time = 2000000

        for monitor in monitors:
            for i in range(2):
                _record_full_pipeline(
                    monitor, f"tid_{i}", "ETHUSDT", "Strategy",
                    base_time + i * 1000, latencies
                )

        ests = [m.get_latency_estimate("ETHUSDT") for m in monitors]
        assert ests[0] == ests[1]


# ══════════════════════════════════════════════════════════════
# SECTION 3: State Persistence (3 tests)
# ══════════════════════════════════════════════════════════════

class TestStatePersistence:
    """Tests for get_state() / restore_state() replay persistence."""

    def test_get_state_returns_symbol_latencies_and_ema(self):
        """get_state returns symbol_latencies and symbol_ema."""
        config = LatencyConfig(
            default_latency_ms=2000,
            min_latency_observations=2,
        )
        monitor = LatencyMonitor(config)
        base_time = int(time.time() * 1000)

        latencies = {
            "signal_created→processing_start": 10,
            "processing_start→processing_end": 100,
            "processing_end→risk_decision": 50,
            "risk_decision→execution_request": 20,
            "execution_request→order_submitted": 15,
            "order_submitted→fill_received": 200,
        }

        for i in range(2):
            _record_full_pipeline(
                monitor, f"tid_{i}", "BTCUSDT", "Strategy",
                base_time + i * 1000, latencies
            )

        state = monitor.get_state()
        assert "symbol_latencies" in state
        assert "symbol_ema" in state
        assert "BTCUSDT" in state["symbol_latencies"]
        assert len(state["symbol_latencies"]["BTCUSDT"]) == 2

    def test_restore_state_recovers_latency_estimate(self):
        """restore_state → get_latency_estimate returns same value."""
        config = LatencyConfig(
            default_latency_ms=2000,
            min_latency_observations=2,
            latency_ema_alpha=0.2,
        )

        latencies = {
            "signal_created→processing_start": 10,
            "processing_start→processing_end": 100,
            "processing_end→risk_decision": 50,
            "risk_decision→execution_request": 20,
            "execution_request→order_submitted": 15,
            "order_submitted→fill_received": 200,
        }

        # First monitor: build state
        monitor1 = LatencyMonitor(config)
        base_time = 3000000
        for i in range(2):
            _record_full_pipeline(
                monitor1, f"tid_{i}", "SOLUSDT", "Strategy",
                base_time + i * 1000, latencies
            )
        est1 = monitor1.get_latency_estimate("SOLUSDT")
        state = monitor1.get_state()

        # Second monitor: restore state
        monitor2 = LatencyMonitor(config)
        monitor2.restore_state(state)
        est2 = monitor2.get_latency_estimate("SOLUSDT")

        assert est1 == est2

    def test_round_trip_preserves_symbol_count(self):
        """Round-trip preserves symbol count."""
        config = LatencyConfig(
            default_latency_ms=2000,
            min_latency_observations=1,
        )
        monitor = LatencyMonitor(config)
        base_time = int(time.time() * 1000)

        symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
        latencies = {
            "signal_created→processing_start": 10,
            "processing_start→processing_end": 100,
            "processing_end→risk_decision": 50,
            "risk_decision→execution_request": 20,
            "execution_request→order_submitted": 15,
            "order_submitted→fill_received": 200,
        }

        for i, symbol in enumerate(symbols):
            _record_full_pipeline(
                monitor, f"tid_{i}", symbol, "Strategy",
                base_time + i * 1000, latencies
            )

        state = monitor.get_state()
        assert len(state["symbol_latencies"]) == 3

        monitor2 = LatencyMonitor(config)
        monitor2.restore_state(state)
        state2 = monitor2.get_state()
        assert len(state2["symbol_latencies"]) == 3


# ══════════════════════════════════════════════════════════════
# SECTION 4: Stage Recording (4 tests)
# ══════════════════════════════════════════════════════════════

class TestStageRecording:
    """Tests for record_stage() and pipeline traversal."""

    def test_single_stage_creates_record(self):
        """Single stage creates record."""
        monitor = LatencyMonitor()
        base_time = int(time.time() * 1000)

        monitor.record_stage(
            "trigger_001",
            "BTCUSDT",
            "TestStrategy",
            PipelineStage.SIGNAL_CREATED,
            base_time
        )

        record = monitor.get_record("trigger_001")
        assert record is not None
        assert record.trigger_id == "trigger_001"
        assert record.symbol == "BTCUSDT"
        assert record.strategy_class == "TestStrategy"
        assert PipelineStage.SIGNAL_CREATED.value in record.timestamps

    def test_multiple_stages_accumulate(self):
        """Multiple stages accumulate in same record."""
        monitor = LatencyMonitor()
        base_time = int(time.time() * 1000)

        stages_and_times = [
            (PipelineStage.SIGNAL_CREATED, base_time),
            (PipelineStage.PROCESSING_START, base_time + 10),
            (PipelineStage.PROCESSING_END, base_time + 110),
        ]

        for stage, ts in stages_and_times:
            monitor.record_stage("trigger_002", "ETHUSDT", "Strategy", stage, ts)

        record = monitor.get_record("trigger_002")
        assert len(record.timestamps) == 3
        assert record.timestamps[PipelineStage.SIGNAL_CREATED.value] == base_time
        assert record.timestamps[PipelineStage.PROCESSING_END.value] == base_time + 110

    def test_total_latency_computed(self):
        """Total latency computed (signal → fill)."""
        monitor = LatencyMonitor(
            LatencyConfig(
                default_latency_ms=2000,
                min_latency_observations=1,
            )
        )
        base_time = int(time.time() * 1000)

        record = _record_full_pipeline(monitor, "trigger_003", "SOLUSDT", "Strategy", base_time)

        assert record.total_latency_ms is not None
        assert record.total_latency_ms > 0

    def test_total_latency_none_without_fill(self):
        """Total latency None without fill."""
        monitor = LatencyMonitor()
        base_time = int(time.time() * 1000)

        monitor.record_stage("trigger_004", "BTCUSDT", "Strategy", PipelineStage.SIGNAL_CREATED, base_time)
        monitor.record_stage("trigger_004", "BTCUSDT", "Strategy", PipelineStage.PROCESSING_END, base_time + 100)

        record = monitor.get_record("trigger_004")
        assert record.total_latency_ms is None  # No FILL_RECEIVED


# ══════════════════════════════════════════════════════════════
# SECTION 5: Inter-Stage Latency (3 tests)
# ══════════════════════════════════════════════════════════════

class TestInterStageLatency:
    """Tests for stage_latency() computations."""

    def test_stage_latency_correct_between_two_stages(self):
        """Stage latency correct between two stages."""
        monitor = LatencyMonitor()
        base_time = int(time.time() * 1000)

        t1 = base_time
        t2 = base_time + 100

        monitor.record_stage("trigger_005", "BTCUSDT", "Strategy", PipelineStage.SIGNAL_CREATED, t1)
        monitor.record_stage("trigger_005", "BTCUSDT", "Strategy", PipelineStage.PROCESSING_START, t2)

        record = monitor.get_record("trigger_005")
        latency = record.stage_latency(PipelineStage.SIGNAL_CREATED, PipelineStage.PROCESSING_START)
        assert latency == 100

    def test_stage_latency_none_for_missing_stage(self):
        """Stage latency None for missing stage."""
        monitor = LatencyMonitor()
        base_time = int(time.time() * 1000)

        monitor.record_stage("trigger_006", "BTCUSDT", "Strategy", PipelineStage.SIGNAL_CREATED, base_time)

        record = monitor.get_record("trigger_006")
        latency = record.stage_latency(PipelineStage.SIGNAL_CREATED, PipelineStage.PROCESSING_START)
        assert latency is None  # PROCESSING_START not recorded

    def test_to_dict_includes_total(self):
        """to_dict includes total."""
        monitor = LatencyMonitor(
            LatencyConfig(
                default_latency_ms=2000,
                min_latency_observations=1,
            )
        )
        base_time = int(time.time() * 1000)

        record = _record_full_pipeline(monitor, "trigger_007", "ETHUSDT", "Strategy", base_time)

        d = record.to_dict()
        assert "total_latency_ms" in d
        assert d["total_latency_ms"] is not None
        assert d["total_latency_ms"] > 0


# ══════════════════════════════════════════════════════════════
# SECTION 6: Threshold Alerts (5 tests)
# ══════════════════════════════════════════════════════════════

class TestThresholdAlerts:
    """Tests for latency threshold alerts."""

    def test_no_alert_within_threshold(self):
        """No alert within threshold."""
        config = LatencyConfig(
            stage_thresholds_ms={
                "signal_created→processing_start": 1000,
            }
        )
        monitor = LatencyMonitor(config)
        base_time = int(time.time() * 1000)

        alert = monitor.record_stage(
            "trigger_008",
            "BTCUSDT",
            "Strategy",
            PipelineStage.SIGNAL_CREATED,
            base_time
        )
        assert alert is None

        alert = monitor.record_stage(
            "trigger_008",
            "BTCUSDT",
            "Strategy",
            PipelineStage.PROCESSING_START,
            base_time + 100  # 100ms, threshold is 1000ms
        )
        assert alert is None

    def test_alert_on_threshold_breach(self):
        """Alert on threshold breach."""
        config = LatencyConfig(
            stage_thresholds_ms={
                "signal_created→processing_start": 100,
            }
        )
        monitor = LatencyMonitor(config)
        base_time = int(time.time() * 1000)

        monitor.record_stage("trigger_009", "BTCUSDT", "Strategy", PipelineStage.SIGNAL_CREATED, base_time)
        alert = monitor.record_stage(
            "trigger_009",
            "BTCUSDT",
            "Strategy",
            PipelineStage.PROCESSING_START,
            base_time + 150  # 150ms, threshold is 100ms
        )

        assert alert is not None
        assert alert.trigger_id == "trigger_009"
        assert alert.stage_pair == "signal_created→processing_start"
        assert alert.latency_ms == 150
        assert alert.threshold_ms == 100

    def test_total_pipeline_alert(self):
        """Total pipeline alert."""
        config = LatencyConfig(
            default_latency_ms=2000,
            min_latency_observations=1,
            total_threshold_ms=500,  # Very low for testing
        )
        monitor = LatencyMonitor(config)
        base_time = int(time.time() * 1000)

        # Record a full pipeline
        monitor.record_stage("trigger_010", "BTCUSDT", "Strategy", PipelineStage.SIGNAL_CREATED, base_time)
        monitor.record_stage("trigger_010", "BTCUSDT", "Strategy", PipelineStage.PROCESSING_START, base_time + 10)
        monitor.record_stage("trigger_010", "BTCUSDT", "Strategy", PipelineStage.PROCESSING_END, base_time + 110)
        monitor.record_stage("trigger_010", "BTCUSDT", "Strategy", PipelineStage.RISK_DECISION, base_time + 160)
        monitor.record_stage("trigger_010", "BTCUSDT", "Strategy", PipelineStage.EXECUTION_REQUEST, base_time + 180)
        monitor.record_stage("trigger_010", "BTCUSDT", "Strategy", PipelineStage.ORDER_SUBMITTED, base_time + 195)
        alert = monitor.record_stage(
            "trigger_010",
            "BTCUSDT",
            "Strategy",
            PipelineStage.FILL_RECEIVED,
            base_time + 600  # Total = 600ms, threshold = 500ms
        )

        assert alert is not None
        assert alert.stage_pair == "total_pipeline"
        assert alert.latency_ms == 600

    def test_stale_fill_flagged(self):
        """Stale fill flagged."""
        config = LatencyConfig(
            default_latency_ms=2000,
            min_latency_observations=1,
            stale_fill_ms=1000,
        )
        monitor = LatencyMonitor(config)
        base_time = int(time.time() * 1000)

        monitor.record_stage("trigger_011", "BTCUSDT", "Strategy", PipelineStage.SIGNAL_CREATED, base_time)
        alert = monitor.record_stage(
            "trigger_011",
            "BTCUSDT",
            "Strategy",
            PipelineStage.PROCESSING_START,
            base_time + 1500  # 1500ms > stale_fill_ms threshold
        )

        assert alert is not None
        assert alert.is_stale is True

    def test_alerts_filterable_by_trigger_id(self):
        """Alerts filterable by trigger_id."""
        config = LatencyConfig(
            stage_thresholds_ms={
                "signal_created→processing_start": 50,
            }
        )
        monitor = LatencyMonitor(config)
        base_time = int(time.time() * 1000)

        # Create alerts for two triggers
        for tid in ["trigger_012", "trigger_013"]:
            monitor.record_stage(tid, "BTCUSDT", "Strategy", PipelineStage.SIGNAL_CREATED, base_time)
            monitor.record_stage(
                tid,
                "BTCUSDT",
                "Strategy",
                PipelineStage.PROCESSING_START,
                base_time + 100
            )

        alerts_012 = monitor.get_alerts("trigger_012")
        alerts_013 = monitor.get_alerts("trigger_013")

        assert len(alerts_012) == 1
        assert len(alerts_013) == 1
        assert alerts_012[0].trigger_id == "trigger_012"
        assert alerts_013[0].trigger_id == "trigger_013"


# ══════════════════════════════════════════════════════════════
# SECTION 7: Finalization & Statistics (3 tests)
# ══════════════════════════════════════════════════════════════

class TestFinalizationAndStatistics:
    """Tests for finalization and statistics computation."""

    def test_fill_moves_record_to_completed(self):
        """Fill moves record to completed."""
        config = LatencyConfig(
            default_latency_ms=2000,
            min_latency_observations=1,
        )
        monitor = LatencyMonitor(config)
        base_time = int(time.time() * 1000)

        _record_full_pipeline(monitor, "trigger_014", "BTCUSDT", "Strategy", base_time)

        # Record should be moved to completed
        record = monitor.get_record("trigger_014")
        assert record is not None
        assert record.trigger_id == "trigger_014"

        # Active should be empty for this trigger (moved to completed)
        snapshot = monitor.snapshot()
        assert snapshot["completed_count"] == 1

    def test_statistics_computed_from_completed(self):
        """Statistics computed from completed."""
        config = LatencyConfig(
            default_latency_ms=2000,
            min_latency_observations=1,
        )
        monitor = LatencyMonitor(config)
        base_time = int(time.time() * 1000)

        # Record 3 pipelines
        for i in range(3):
            _record_full_pipeline(
                monitor, f"trigger_{i:03d}", "BTCUSDT", "Strategy",
                base_time + i * 1000
            )

        stats = monitor.get_statistics()
        assert "total_pipeline" in stats
        assert stats["total_pipeline"]["count"] == 3
        assert stats["total_pipeline"]["mean_ms"] > 0
        assert stats["total_pipeline"]["median_ms"] > 0

    def test_empty_stats_with_no_completions(self):
        """Empty stats with no completions."""
        monitor = LatencyMonitor()
        base_time = int(time.time() * 1000)

        # Record partial pipeline
        monitor.record_stage("trigger_015", "BTCUSDT", "Strategy", PipelineStage.SIGNAL_CREATED, base_time)

        stats = monitor.get_statistics()
        assert stats == {}


# ══════════════════════════════════════════════════════════════
# SECTION 8: Cleanup (2 tests)
# ══════════════════════════════════════════════════════════════

class TestCleanup:
    """Tests for cleanup_stale() functionality."""

    def test_cleanup_removes_stale_active(self):
        """Cleanup removes stale active."""
        monitor = LatencyMonitor()
        base_time = int(time.time() * 1000)

        # Record an active record
        monitor.record_stage("trigger_016", "BTCUSDT", "Strategy", PipelineStage.SIGNAL_CREATED, base_time - 400_000)

        snapshot_before = monitor.snapshot()
        assert snapshot_before["active_count"] == 1

        # Cleanup with max_age_ms = 300_000 (300s)
        removed = monitor.cleanup_stale(max_age_ms=300_000)

        assert removed == 1
        snapshot_after = monitor.snapshot()
        assert snapshot_after["active_count"] == 0

    def test_cleanup_preserves_completed(self):
        """Cleanup preserves completed."""
        config = LatencyConfig(
            default_latency_ms=2000,
            min_latency_observations=1,
        )
        monitor = LatencyMonitor(config)
        base_time = int(time.time() * 1000)

        # Record completed pipelines
        for i in range(2):
            _record_full_pipeline(
                monitor, f"trigger_{i:03d}", "BTCUSDT", "Strategy",
                base_time - 400_000 + i * 1000
            )

        snapshot_before = monitor.snapshot()
        assert snapshot_before["completed_count"] == 2

        # Cleanup active (none should be active)
        removed = monitor.cleanup_stale(max_age_ms=300_000)
        assert removed == 0

        snapshot_after = monitor.snapshot()
        assert snapshot_after["completed_count"] == 2


# ══════════════════════════════════════════════════════════════
# SECTION 9: Snapshot (2 tests)
# ══════════════════════════════════════════════════════════════

class TestSnapshot:
    """Tests for snapshot() output."""

    def test_snapshot_has_expected_keys(self):
        """Snapshot has expected keys."""
        config = LatencyConfig(
            default_latency_ms=2000,
            min_latency_observations=1,
        )
        monitor = LatencyMonitor(config)
        base_time = int(time.time() * 1000)

        _record_full_pipeline(monitor, "trigger_017", "BTCUSDT", "Strategy", base_time)

        snapshot = monitor.snapshot()
        assert "active_count" in snapshot
        assert "completed_count" in snapshot
        assert "alert_count" in snapshot
        assert "symbol_estimates_count" in snapshot
        assert "statistics" in snapshot

    def test_snapshot_reflects_state(self):
        """Snapshot reflects state."""
        config = LatencyConfig(
            default_latency_ms=2000,
            min_latency_observations=1,
        )
        monitor = LatencyMonitor(config)
        base_time = int(time.time() * 1000)

        # Record pipeline
        _record_full_pipeline(monitor, "trigger_018", "BTCUSDT", "Strategy", base_time)

        snapshot = monitor.snapshot()
        assert snapshot["completed_count"] == 1
        assert snapshot["symbol_estimates_count"] == 1
        assert "total_pipeline" in snapshot["statistics"]
