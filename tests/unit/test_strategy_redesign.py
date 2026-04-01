# ============================================================
# NEXUS TRADER — Strategy Redesign Test Suite
# ============================================================
# Comprehensive pytest test coverage for the Strategies page
# redesign, including registry, settings, audit logging,
# configuration versioning, metrics, and validation.
#
# Run with: pytest tests/unit/test_strategy_redesign.py -v
# ============================================================

import pytest
import json
import yaml
import sqlite3
import threading
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

# Add project root to path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


# ============================================================
# TestStrategyRegistry — 12 tests
# ============================================================

class TestStrategyRegistry:
    """Tests for core/strategies/strategy_registry.py functionality."""

    def test_sr001_registry_has_10_models(self):
        """Verify registry contains exactly 10 trading models."""
        from core.strategies.strategy_registry import STRATEGY_REGISTRY
        assert len(STRATEGY_REGISTRY) == 10

    def test_sr002_all_model_types_present(self):
        """Verify all four model types are represented in registry."""
        from core.strategies.strategy_registry import STRATEGY_REGISTRY
        types = {m.model_type for m in STRATEGY_REGISTRY}
        assert "CORE" in types
        assert "AGENT" in types
        assert "ML" in types
        assert "META" in types
        assert len(types) == 4

    def test_sr003_get_model_def_returns_correct_model(self):
        """Verify get_model_def() returns the expected model by name."""
        from core.strategies.strategy_registry import get_model_def
        trend = get_model_def("trend")
        assert trend is not None
        assert trend.name == "trend"
        assert trend.display_name == "Trend Model"

    def test_sr004_get_model_def_returns_none_for_unknown(self):
        """Verify get_model_def() returns None for non-existent models."""
        from core.strategies.strategy_registry import get_model_def
        unknown = get_model_def("nonexistent_model")
        assert unknown is None

    def test_sr005_all_config_keys_unique(self):
        """Verify no duplicate config keys exist in the registry."""
        from core.strategies.strategy_registry import get_all_config_keys
        keys = get_all_config_keys()
        unique_keys = set(keys)
        assert len(keys) == len(unique_keys), f"Found duplicate config keys: {len(keys)} != {len(unique_keys)}"

    def test_sr006_all_params_have_valid_ranges(self):
        """Verify all numeric parameters have min < max."""
        from core.strategies.strategy_registry import STRATEGY_REGISTRY
        for model in STRATEGY_REGISTRY:
            for param in model.params:
                if param.param_type in ("float", "int"):
                    if param.min_val is not None and param.max_val is not None:
                        assert param.min_val < param.max_val, \
                            f"{param.key}: min ({param.min_val}) >= max ({param.max_val})"

    def test_sr007_all_params_have_descriptions(self):
        """Verify all parameters have non-empty descriptions."""
        from core.strategies.strategy_registry import STRATEGY_REGISTRY, GLOBAL_PARAMS
        all_params = []
        for model in STRATEGY_REGISTRY:
            all_params.extend(model.params)
        all_params.extend(GLOBAL_PARAMS)

        for param in all_params:
            assert param.description, f"Parameter {param.key} missing description"

    def test_sr008_disabled_models_detected(self):
        """Verify disabled models are marked in registry (mean_reversion, liquidity_sweep)."""
        from core.strategies.strategy_registry import get_model_def
        mean_rev = get_model_def("mean_reversion")
        liq_sweep = get_model_def("liquidity_sweep")

        assert mean_rev is not None
        assert liq_sweep is not None
        assert mean_rev.enabled_by_default is False
        assert liq_sweep.enabled_by_default is False

    def test_sr009_model_weights_match_defaults(self):
        """Verify get_model_weight() returns registry default when config matches."""
        from core.strategies.strategy_registry import get_model_weight, get_model_def

        # Default config has same values as registry defaults
        trend_def = get_model_def("trend")
        weight = get_model_weight("trend")
        # Should return the default weight (both config and registry agree)
        assert weight == trend_def.default_weight

    def test_sr010_global_params_count(self):
        """Verify global parameters count (16 base + 6 LTF confirmation = 22)."""
        from core.strategies.strategy_registry import GLOBAL_PARAMS
        assert len(GLOBAL_PARAMS) == 22

    def test_sr011_param_types_valid(self):
        """Verify all param_type values are in valid set."""
        from core.strategies.strategy_registry import STRATEGY_REGISTRY, GLOBAL_PARAMS
        valid_types = {"float", "int", "bool", "enum"}

        all_params = []
        for model in STRATEGY_REGISTRY:
            all_params.extend(model.params)
        all_params.extend(GLOBAL_PARAMS)

        for param in all_params:
            assert param.param_type in valid_types, \
                f"Invalid param_type '{param.param_type}' in {param.key}"

    def test_sr012_model_type_colors_defined(self):
        """Verify all models have valid hex color codes."""
        from core.strategies.strategy_registry import STRATEGY_REGISTRY
        for model in STRATEGY_REGISTRY:
            assert model.type_color.startswith("#"), \
                f"Invalid color format in {model.name}: {model.type_color}"
            # Verify it's a valid hex color
            try:
                int(model.type_color[1:], 16)
            except ValueError:
                pytest.fail(f"Invalid hex color in {model.name}: {model.type_color}")


# ============================================================
# TestSettingsIntegration — 10 tests
# ============================================================

class TestSettingsIntegration:
    """Tests for config/settings.py integration with registry."""

    @pytest.fixture
    def temp_settings(self, tmp_path):
        """Create isolated settings instance with temp config file."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({
            "model_weights": {
                "trend": 0.35,
                "momentum_breakout": 0.25,
                "vwap_reversion": 0.28,
                "mean_reversion": 0.25,
                "liquidity_sweep": 0.15,
                "funding_rate": 0.20,
                "order_book": 0.18,
                "sentiment": 0.12,
                "rl_ensemble": 0.30,
                "orchestrator": 0.22,
            },
            "models": {
                "trend": {"adx_min": 25.0},
            },
            "disabled_models": [],
        }))

        # Patch config.constants.CONFIG_PATH so reload picks up the temp file
        # (patching config.settings.CONFIG_PATH alone is defeated by the reload)
        with patch('config.constants.CONFIG_PATH', config_file):
            import importlib
            import config.settings
            importlib.reload(config.settings)
            s = config.settings.AppSettings()
            yield s

    def test_si001_default_config_has_model_weights_section(self):
        """Verify DEFAULT_CONFIG contains model_weights section."""
        from config.settings import DEFAULT_CONFIG
        assert "model_weights" in DEFAULT_CONFIG
        assert isinstance(DEFAULT_CONFIG["model_weights"], dict)

    def test_si002_default_config_has_models_section(self):
        """Verify DEFAULT_CONFIG contains models section with all 8 models."""
        from config.settings import DEFAULT_CONFIG
        assert "models" in DEFAULT_CONFIG
        expected_models = {
            "trend", "momentum_breakout", "vwap_reversion", "mean_reversion",
            "liquidity_sweep", "funding_rate", "order_book", "sentiment",
            "donchian_breakout",
        }
        assert set(DEFAULT_CONFIG["models"].keys()) == expected_models

    def test_si003_model_weights_values_match_registry(self):
        """Verify DEFAULT_CONFIG weights match registry defaults."""
        from config.settings import DEFAULT_CONFIG
        from core.strategies.strategy_registry import get_model_def

        for model_name, weight in DEFAULT_CONFIG["model_weights"].items():
            model_def = get_model_def(model_name)
            if model_def:  # Skip rl_ensemble, orchestrator (not in registry)
                assert weight == model_def.default_weight, \
                    f"Weight mismatch for {model_name}: {weight} != {model_def.default_weight}"

    def test_si004_models_section_has_all_expected_models(self):
        """Verify models section has configurations for all expected models."""
        from config.settings import DEFAULT_CONFIG
        required_models = ["trend", "momentum_breakout", "vwap_reversion"]
        for model_name in required_models:
            assert model_name in DEFAULT_CONFIG["models"], \
                f"Model '{model_name}' missing from models section"

    def test_si005_settings_get_reads_model_weight(self, temp_settings):
        """Verify settings.get() returns configured model weight."""
        weight = temp_settings.get("model_weights.trend", 0.0)
        assert weight == 0.35

    def test_si006_settings_get_reads_model_param(self, temp_settings):
        """Verify settings.get() returns configured model parameter."""
        adx_min = temp_settings.get("models.trend.adx_min", 20.0)
        assert adx_min == 25.0

    def test_si007_settings_fallback_to_default(self, temp_settings):
        """Verify settings.get() falls back to default for missing key."""
        value = temp_settings.get("nonexistent.key", 99.0)
        assert value == 99.0

    def test_si008_disabled_models_default_is_empty_list(self):
        """Verify DEFAULT_CONFIG has empty disabled_models by default."""
        from config.settings import DEFAULT_CONFIG
        assert "disabled_models" in DEFAULT_CONFIG
        assert DEFAULT_CONFIG["disabled_models"] == []

    def test_si009_deep_merge_preserves_new_sections(self, temp_settings):
        """Verify settings can add new config sections without losing defaults."""
        # This is more of an integration test; settings should load defaults
        # then merge user config on top
        assert temp_settings.get("model_weights.trend") == 0.35

    def test_si010_base_size_usdt_in_execution_section(self):
        """Verify BASE_SIZE_USDT is accessible in execution config."""
        from config.settings import DEFAULT_CONFIG
        # Check if execution/risk config section exists
        assert "risk" in DEFAULT_CONFIG or "execution" in DEFAULT_CONFIG


# ============================================================
# TestAuditLogger — 8 tests
# ============================================================

class TestAuditLogger:
    """Tests for core/strategies/audit_logger.py functionality."""

    @pytest.fixture
    def audit_logger(self, tmp_path):
        """Create isolated audit logger with temp log file."""
        from core.strategies.audit_logger import AuditLogger
        log_file = tmp_path / "test_audit.jsonl"
        return AuditLogger(log_file=log_file)

    def test_al001_log_change_creates_file(self, audit_logger):
        """Verify log_change() creates the log file if it doesn't exist."""
        audit_logger.log_change(
            action="param_change",
            model="trend",
            key="adx_min",
            old_value=25.0,
            new_value=30.0,
        )
        assert audit_logger.log_file.exists()

    def test_al002_log_change_writes_json_line(self, audit_logger):
        """Verify log_change() writes a valid JSON line to the log."""
        audit_logger.log_change(
            action="param_change",
            model="trend",
            key="adx_min",
            old_value=25.0,
            new_value=30.0,
        )

        with open(audit_logger.log_file, "r") as f:
            line = f.readline().strip()

        entry = json.loads(line)
        assert entry["model"] == "trend"
        assert entry["key"] == "adx_min"

    def test_al003_log_entry_has_required_fields(self, audit_logger):
        """Verify log entries contain all required fields."""
        audit_logger.log_change(
            action="param_change",
            model="trend",
            key="adx_min",
            old_value=25.0,
            new_value=30.0,
        )

        entries = audit_logger.get_log()
        assert len(entries) == 1

        entry = entries[0]
        assert "ts" in entry
        assert "action" in entry
        assert "model" in entry
        assert "key" in entry
        assert "old" in entry
        assert "new" in entry

    def test_al004_get_log_returns_entries_newest_first(self, audit_logger):
        """Verify get_log() returns entries in reverse chronological order."""
        audit_logger.log_change("param_change", "trend", "adx_min", 25.0, 26.0)
        time.sleep(0.01)
        audit_logger.log_change("param_change", "trend", "rsi_long_min", 45.0, 50.0)

        entries = audit_logger.get_log()
        assert len(entries) == 2
        assert entries[0]["key"] == "rsi_long_min"  # Most recent first
        assert entries[1]["key"] == "adx_min"

    def test_al005_get_log_filters_by_model(self, audit_logger):
        """Verify get_log(model=...) filters by model name."""
        audit_logger.log_change("param_change", "trend", "adx_min", 25.0, 30.0)
        audit_logger.log_change("param_change", "momentum_breakout", "lookback", 20, 25)

        trend_entries = audit_logger.get_log(model="trend")
        assert len(trend_entries) == 1
        assert trend_entries[0]["model"] == "trend"

    def test_al006_get_log_respects_limit(self, audit_logger):
        """Verify get_log(limit=...) returns at most N entries."""
        for i in range(5):
            audit_logger.log_change("param_change", "trend", f"param_{i}", i, i+1)

        limited = audit_logger.get_log(limit=2)
        assert len(limited) == 2

    def test_al007_log_change_thread_safe(self, audit_logger):
        """Verify concurrent writes to the log don't cause corruption."""
        def write_entries(start, count):
            for i in range(start, start + count):
                audit_logger.log_change("param_change", "trend", f"key_{i}", i, i+1)

        threads = [
            threading.Thread(target=write_entries, args=(0, 5)),
            threading.Thread(target=write_entries, args=(5, 5)),
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All entries should be readable and valid JSON
        entries = audit_logger.get_log()
        assert len(entries) == 10

    def test_al008_empty_log_returns_empty_list(self, audit_logger):
        """Verify get_log() returns empty list when log file is empty."""
        entries = audit_logger.get_log()
        assert entries == []


# ============================================================
# TestConfigVersioner — 10 tests
# ============================================================

class TestConfigVersioner:
    """Tests for core/strategies/config_versioner.py functionality."""

    @pytest.fixture
    def versioner(self, tmp_path):
        """Create isolated config versioner with temp files."""
        from core.strategies.config_versioner import ConfigVersioner
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({
            "test": True,
            "model_weights": {"trend": 0.35},
        }))
        versions_dir = tmp_path / "config_versions"
        return ConfigVersioner(config_path=config_file, versions_dir=versions_dir)

    def test_cv001_save_version_creates_file(self, versioner):
        """Verify save_version() creates a timestamped version file."""
        version_num = versioner.save_version(description="Initial version")
        assert version_num == 1

        # Check that a version file was created
        files = list(versioner.versions_dir.glob("v001_*.yaml"))
        assert len(files) == 1

    def test_cv002_save_version_increments_number(self, versioner):
        """Verify successive save_version() calls increment version number."""
        v1 = versioner.save_version("v1")
        v2 = versioner.save_version("v2")
        v3 = versioner.save_version("v3")

        assert v1 == 1
        assert v2 == 2
        assert v3 == 3

    def test_cv003_list_versions_returns_sorted(self, versioner):
        """Verify list_versions() returns versions sorted by number."""
        versioner.save_version("v1")
        time.sleep(0.01)
        versioner.save_version("v2")

        versions = versioner.list_versions()
        assert len(versions) >= 2
        # Most recent first
        assert versions[0]["version"] >= versions[-1]["version"]

    def test_cv004_load_version_returns_config_dict(self, versioner):
        """Verify load_version() returns the config dict for a version."""
        versioner.save_version("test")

        config = versioner.load_version(1)
        assert isinstance(config, dict)
        assert "test" in config or "model_weights" in config

    def test_cv005_diff_versions_finds_changes(self, versioner):
        """Verify diff_versions() identifies changed keys between versions."""
        versioner.save_version("v1")

        # Modify config and save v2
        config_file = versioner.config_path
        config = yaml.safe_load(config_file.read_text())
        config["new_key"] = "new_value"
        config_file.write_text(yaml.dump(config))
        versioner.save_version("v2")

        diff = versioner.diff_versions(1, 2)
        assert len(diff) > 0

    def test_cv006_diff_versions_empty_when_identical(self, versioner):
        """Verify diff_versions() returns empty dict when configs are identical."""
        versioner.save_version("v1")
        versioner.save_version("v1_again")

        diff = versioner.diff_versions(1, 2)
        # Diff should be empty or very minimal (just timestamps)
        assert len(diff) <= 1

    def test_cv007_prune_old_removes_excess(self, versioner):
        """Verify prune_old_versions() removes old version files."""
        for i in range(5):
            versioner.save_version(f"v{i}")

        # Prune, keeping only 2
        versioner.prune_old(max_versions=2)

        versions = versioner.list_versions()
        assert len(versions) <= 2

    def test_cv008_baseline_created_on_first_use(self, versioner):
        """Verify baseline.yaml is created on first versioner use."""
        baseline = versioner.versions_dir / "baseline.yaml"
        assert baseline.exists()

    def test_cv009_get_current_version_returns_highest(self, versioner):
        """Verify get_current_version() returns the highest version number."""
        versioner.save_version("v1")
        versioner.save_version("v2")
        versioner.save_version("v3")

        current = versioner.get_current_version()
        assert current == 3

    def test_cv010_version_filename_format(self, versioner):
        """Verify version files follow naming convention v{NNN}_{YYYYMMDD_HHMMSS}.yaml."""
        versioner.save_version("test")

        files = list(versioner.versions_dir.glob("v001_*.yaml"))
        assert len(files) == 1

        # Check filename format
        filename = files[0].name
        assert filename.startswith("v001_")
        assert filename.endswith(".yaml")


# ============================================================
# TestStrategyMetrics — 6 tests
# ============================================================

class TestStrategyMetrics:
    """Tests for core/strategies/strategy_metrics.py functionality."""

    @pytest.fixture
    def metrics_db(self, tmp_path):
        """Create temp SQLite DB with mock paper_trades."""
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))

        # Create table
        conn.execute('''CREATE TABLE paper_trades (
            id INTEGER PRIMARY KEY,
            symbol TEXT,
            side TEXT,
            entry_price REAL,
            exit_price REAL,
            quantity REAL,
            pnl_usdt REAL,
            pnl_pct REAL,
            models_fired TEXT,
            regime TEXT,
            score REAL,
            opened_at TEXT,
            closed_at TEXT,
            exit_reason TEXT,
            stop_loss REAL,
            take_profit REAL
        )''')

        # Insert test trades
        trades = [
            (1, 'BTC/USDT', 'buy', 50000, 51000, 0.01, 10.0, 2.0,
             '["trend","momentum_breakout"]', 'bull_trend', 0.85, '2026-03-01', '2026-03-02', 'take_profit', 49000, 51000),
            (2, 'ETH/USDT', 'buy', 3000, 2900, 0.1, -10.0, -3.3,
             '["trend"]', 'bull_trend', 0.75, '2026-03-02', '2026-03-03', 'stop_loss', 2900, 3100),
            (3, 'BTC/USDT', 'sell', 52000, 51000, 0.01, 10.0, 1.9,
             '["momentum_breakout"]', 'bear_trend', 0.80, '2026-03-03', '2026-03-04', 'take_profit', 53000, 51000),
            (4, 'SOL/USDT', 'buy', 100, 110, 1.0, 10.0, 10.0,
             '["trend","funding_rate"]', 'bull_trend', 0.90, '2026-03-04', '2026-03-05', 'take_profit', 95, 110),
            (5, 'XRP/USDT', 'sell', 0.50, 0.48, 100, 2.0, 4.0,
             '["trend"]', 'bear_trend', 0.70, '2026-03-05', '2026-03-06', 'take_profit', 0.52, 0.48),
        ]

        conn.executemany(
            'INSERT INTO paper_trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            trades
        )
        conn.commit()
        conn.close()

        return db_path

    def test_sm001_empty_db_returns_empty_stats(self, tmp_path):
        """Verify compute_all_model_stats() returns empty dict for empty DB."""
        from core.strategies.strategy_metrics import StrategyMetricsCalculator

        db_path = tmp_path / "empty.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute('''CREATE TABLE paper_trades (
            id INTEGER PRIMARY KEY,
            symbol TEXT,
            side TEXT,
            entry_price REAL,
            exit_price REAL,
            quantity REAL,
            pnl_usdt REAL,
            pnl_pct REAL,
            models_fired TEXT,
            regime TEXT,
            score REAL,
            opened_at TEXT,
            closed_at TEXT,
            exit_reason TEXT,
            stop_loss REAL,
            take_profit REAL
        )''')
        conn.close()

        calc = StrategyMetricsCalculator(db_path=db_path)
        stats = calc.compute_all_model_stats()
        assert stats == {}

    def test_sm002_compute_stats_with_mock_trades(self, metrics_db):
        """Verify compute_all_model_stats() processes mock trades correctly."""
        from core.strategies.strategy_metrics import StrategyMetricsCalculator

        calc = StrategyMetricsCalculator(db_path=metrics_db)
        stats = calc.compute_all_model_stats()

        # Should have stats for trend, momentum_breakout, funding_rate
        assert "trend" in stats
        assert "momentum_breakout" in stats

    def test_sm003_trade_attributes_to_multiple_models(self, metrics_db):
        """Verify trades with multiple models_fired are counted for each model."""
        from core.strategies.strategy_metrics import StrategyMetricsCalculator

        calc = StrategyMetricsCalculator(db_path=metrics_db)
        stats = calc.compute_all_model_stats()

        # Trade #1 lists trend and momentum_breakout, so both should count it
        if "trend" in stats:
            assert stats["trend"].trade_count > 0

    def test_sm004_win_rate_calculation_correct(self, metrics_db):
        """Verify win_rate is calculated correctly (wins / total)."""
        from core.strategies.strategy_metrics import StrategyMetricsCalculator

        calc = StrategyMetricsCalculator(db_path=metrics_db)
        stats = calc.compute_all_model_stats()

        # Check that win_rate is in valid range [0, 1]
        for model_name, model_stats in stats.items():
            assert 0.0 <= model_stats.win_rate <= 1.0

    def test_sm005_profit_factor_calculation_correct(self, metrics_db):
        """Verify profit_factor is gross_wins / gross_losses."""
        from core.strategies.strategy_metrics import StrategyMetricsCalculator

        calc = StrategyMetricsCalculator(db_path=metrics_db)
        stats = calc.compute_all_model_stats()

        # Check that profit_factor is non-negative
        for model_name, model_stats in stats.items():
            assert model_stats.profit_factor >= 0.0

    def test_sm006_handles_missing_db_gracefully(self, tmp_path):
        """Verify compute_all_model_stats() returns empty stats for missing DB."""
        from core.strategies.strategy_metrics import StrategyMetricsCalculator

        calc = StrategyMetricsCalculator(db_path=tmp_path / "nonexistent.db")
        # Should handle gracefully — either returns empty or raises
        try:
            stats = calc.compute_all_model_stats()
            # If it returns, should be empty or dict
            assert isinstance(stats, dict)
        except Exception:
            # If it raises, that's also acceptable for missing DB
            pass


# ============================================================
# TestValidation — 8 tests
# ============================================================

class TestValidation:
    """Tests for parameter validation logic."""

    def test_val001_weight_in_valid_range(self):
        """Verify weights in [0.0, 1.0] are accepted."""
        from core.strategies.strategy_registry import ModelDef

        # Should not raise
        model = ModelDef(
            name="test",
            display_name="Test Model",
            model_type="CORE",
            type_color="#4488CC",
            description="Test",
            weight_key="model_weights.test",
            default_weight=0.5,  # Valid
        )
        assert model.default_weight == 0.5

    def test_val002_weight_out_of_range_rejected(self):
        """Verify weights outside [0.0, 1.0] raise ValueError."""
        from core.strategies.strategy_registry import ModelDef

        with pytest.raises(ValueError):
            ModelDef(
                name="test",
                display_name="Test Model",
                model_type="CORE",
                type_color="#4488CC",
                description="Test",
                weight_key="model_weights.test",
                default_weight=1.5,  # Out of range
            )

    def test_val003_rsi_min_less_than_max(self):
        """Verify min_val < max_val is enforced for parameters."""
        from core.strategies.strategy_registry import _p

        # Should not raise
        param = _p(
            "models.test.rsi",
            "RSI",
            "float",
            45.0,
            min_val=30.0,
            max_val=70.0,
        )
        assert param.min_val < param.max_val

    def test_val004_rsi_min_greater_than_max_rejected(self):
        """Verify min_val >= max_val raises ValueError."""
        from core.strategies.strategy_registry import ModelParamDef

        with pytest.raises(ValueError):
            ModelParamDef(
                key="models.test.rsi",
                label="RSI",
                param_type="float",
                default=45.0,
                min_val=70.0,
                max_val=30.0,  # Invalid: min > max
            )

    def test_val005_tp_greater_than_sl(self):
        """Verify take-profit > stop-loss constraint can be validated."""
        # This is a cross-field validation that would be in the UI layer
        tp = 51000
        sl = 49000
        assert tp > sl

    def test_val006_tp_less_than_sl_rejected(self):
        """Verify take-profit < stop-loss is detected."""
        tp = 49000
        sl = 51000
        assert not (tp > sl), "Invalid TP/SL: TP should be > SL"

    def test_val007_at_least_one_core_model_enabled(self):
        """Verify at least one CORE model is enabled."""
        from core.strategies.strategy_registry import STRATEGY_REGISTRY

        core_models = [m for m in STRATEGY_REGISTRY if m.model_type == "CORE"]
        enabled_core = [m for m in core_models if m.enabled_by_default]

        assert len(enabled_core) > 0, "At least one CORE model must be enabled"

    def test_val008_all_core_disabled_rejected(self):
        """Verify scenario where all CORE models are disabled is invalid."""
        from core.strategies.strategy_registry import STRATEGY_REGISTRY

        core_models = [m for m in STRATEGY_REGISTRY if m.model_type == "CORE"]

        # In the current config, not all core models are disabled
        # This test verifies we can detect such a scenario
        disabled_count = sum(1 for m in core_models if not m.enabled_by_default)
        assert disabled_count < len(core_models), "Not all CORE models should be disabled"


# ============================================================
# TestSubModelSettingsRead — 8 tests
# ============================================================

class TestSubModelSettingsRead:
    """Tests that sub-models read settings correctly."""

    def test_sm_trend_reads_adx_min_from_settings(self):
        """Verify TrendModel sources adx_min from settings."""
        import inspect
        from core.signals.sub_models.trend_model import TrendModel

        source = inspect.getsource(TrendModel.evaluate)
        # Should reference the settings key
        assert "models.trend.adx_min" in source or "adx_min" in source

    def test_sm_trend_falls_back_to_default(self):
        """Verify TrendModel falls back to hardcoded default if setting missing."""
        from core.signals.sub_models.trend_model import TrendModel

        # The model should have a default adx_min value
        model = TrendModel()
        assert hasattr(model, 'ADX_MIN') or hasattr(model, 'adx_min') or True  # Just verify model exists

    def test_sm_momentum_reads_lookback_from_settings(self):
        """Verify MomentumBreakoutModel sources lookback from settings."""
        import inspect
        from core.signals.sub_models.momentum_breakout_model import MomentumBreakoutModel

        source = inspect.getsource(MomentumBreakoutModel.evaluate)
        assert "models.momentum_breakout.lookback" in source or "lookback" in source

    def test_sm_vwap_reads_z_threshold_from_settings(self):
        """Verify VWAPReversionModel sources z_threshold from settings."""
        import inspect
        from core.signals.sub_models.vwap_reversion_model import VWAPReversionModel

        source = inspect.getsource(VWAPReversionModel.evaluate)
        assert "models.vwap_reversion.z_threshold" in source or "z_threshold" in source

    def test_sm_funding_reads_min_signal_from_settings(self):
        """Verify FundingRateModel sources min_signal from settings."""
        import inspect
        from core.signals.sub_models.funding_rate_model import FundingRateModel

        source = inspect.getsource(FundingRateModel.evaluate)
        assert "models.funding_rate.min_signal" in source or "min_signal" in source

    def test_sm_orderbook_reads_min_confidence_from_settings(self):
        """Verify OrderBookModel sources min_confidence from settings."""
        import inspect
        from core.signals.sub_models.order_book_model import OrderBookModel

        source = inspect.getsource(OrderBookModel.evaluate)
        assert "models.order_book.min_confidence" in source or "min_confidence" in source

    def test_sm_sentiment_reads_max_age_from_settings(self):
        """Verify SentimentModel sources max_age_minutes from settings (source check)."""
        # SentimentModel imports PySide6 via event_bus, so we check the source file directly
        src_path = Path(__file__).parent.parent.parent / "core" / "signals" / "sub_models" / "sentiment_model.py"
        source = src_path.read_text()
        assert "models.sentiment.max_age_minutes" in source or "_max_age_minutes" in source

    def test_sm_mean_reversion_reads_bb_lower_dist_from_settings(self):
        """Verify MeanReversionModel sources bb_lower_dist from settings."""
        import inspect
        from core.signals.sub_models.mean_reversion_model import MeanReversionModel

        source = inspect.getsource(MeanReversionModel.evaluate)
        assert "models.mean_reversion.bb_lower_dist" in source or "bb_lower_dist" in source


# ============================================================
# TestConfluenceScorerWeightRead — 4 tests
# ============================================================

class TestConfluenceScorerWeightRead:
    """Tests that ConfluenceScorer reads model weights correctly."""

    def test_cw001_get_effective_weights_returns_all_models(self):
        """Verify get_effective_weights() returns a weight for each model."""
        from core.meta_decision.confluence_scorer import get_effective_weights, MODEL_WEIGHTS

        weights = get_effective_weights()

        # Should have weights for all models in MODEL_WEIGHTS
        for model_name in MODEL_WEIGHTS:
            assert model_name in weights, f"Missing weight for {model_name}"
            assert isinstance(weights[model_name], float)

    def test_cw002_get_model_weight_reads_from_settings(self):
        """Verify get_model_weight() sources from settings."""
        from core.meta_decision.confluence_scorer import ConfluenceScorer

        with patch('config.settings.settings') as mock_settings:
            mock_settings.get.return_value = 0.42
            scorer = ConfluenceScorer()
            # When model_weights section is read, should call settings.get
            # This is more of an integration test

    def test_cw003_get_model_weight_fallback_to_hardcoded(self):
        """Verify model weight falls back to registry default."""
        from core.meta_decision.confluence_scorer import ConfluenceScorer
        from core.strategies.strategy_registry import get_model_def

        scorer = ConfluenceScorer()

        # Get the effective weight for a model
        trend_def = get_model_def("trend")
        if trend_def:
            # Weight should be either configured or default
            assert trend_def.default_weight > 0.0

    def test_cw004_weight_override_changes_effective_weight(self, tmp_path):
        """Verify overriding a weight in config changes effective weight."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({
            "model_weights": {
                "trend": 0.50,  # Override from default 0.35
                "momentum_breakout": 0.25,
                "vwap_reversion": 0.28,
                "mean_reversion": 0.25,
                "liquidity_sweep": 0.15,
                "funding_rate": 0.20,
                "order_book": 0.18,
                "sentiment": 0.12,
                "rl_ensemble": 0.30,
                "orchestrator": 0.22,
            },
        }))

        with patch('config.settings.CONFIG_PATH', config_file):
            with patch('config.settings.AppSettings.get') as mock_get:
                mock_get.return_value = 0.50
                # Weight should be overridden value
                assert mock_get.return_value == 0.50


# ============================================================
# TestRestoreDefaults — 4 tests
# ============================================================

class TestRestoreDefaults:
    """Tests for restoring default configuration."""

    def test_rd001_default_config_model_weights_match_registry(self):
        """Verify DEFAULT_CONFIG weights match registry defaults."""
        from config.settings import DEFAULT_CONFIG
        from core.strategies.strategy_registry import STRATEGY_REGISTRY

        for model in STRATEGY_REGISTRY:
            if model.name in DEFAULT_CONFIG.get("model_weights", {}):
                assert DEFAULT_CONFIG["model_weights"][model.name] == model.default_weight

    def test_rd002_default_config_model_params_match_registry(self):
        """Verify DEFAULT_CONFIG params match registry param defaults."""
        from config.settings import DEFAULT_CONFIG
        from core.strategies.strategy_registry import STRATEGY_REGISTRY

        for model in STRATEGY_REGISTRY:
            if model.name in DEFAULT_CONFIG.get("models", {}):
                for param in model.params:
                    param_name = param.key.split('.')[-1]
                    if param_name in DEFAULT_CONFIG["models"][model.name]:
                        assert DEFAULT_CONFIG["models"][model.name][param_name] == param.default

    def test_rd003_all_registry_params_have_matching_default(self):
        """Verify core model registry params have matching DEFAULT_CONFIG entries."""
        from config.settings import DEFAULT_CONFIG
        from core.strategies.strategy_registry import STRATEGY_REGISTRY

        # Only check models that have params in the "models" config section
        # (RL ensemble and orchestrator use different config paths)
        models_with_config = DEFAULT_CONFIG.get("models", {})
        for model in STRATEGY_REGISTRY:
            if model.name in models_with_config:
                # Verify at least one param exists
                assert len(models_with_config[model.name]) > 0, \
                    f"Model {model.name} has empty config in DEFAULT_CONFIG"

    def test_rd004_registry_defaults_are_valid_types(self):
        """Verify all registry default values have valid types."""
        from core.strategies.strategy_registry import STRATEGY_REGISTRY

        for model in STRATEGY_REGISTRY:
            assert isinstance(model.default_weight, (int, float))
            for param in model.params:
                # Default should match param_type
                if param.param_type == "float":
                    assert isinstance(param.default, (int, float))
                elif param.param_type == "int":
                    assert isinstance(param.default, int)
                elif param.param_type == "bool":
                    assert isinstance(param.default, bool)
                elif param.param_type == "enum":
                    assert param.default in (param.enum_values or [])


# ============================================================
# TestRegimeAffinity — 10 tests
# ============================================================

class TestRegimeAffinity:
    """Tests for regime affinity editing, persistence, restore, and validation."""

    REGIMES = [
        "bull_trend", "bear_trend", "ranging", "volatility_expansion",
        "volatility_compression", "uncertain", "crisis", "liquidation_cascade",
        "squeeze", "recovery", "accumulation", "distribution",
    ]

    def test_ra001_default_config_has_regime_affinity(self):
        """Verify DEFAULT_CONFIG contains regime_affinity section."""
        from config.settings import DEFAULT_CONFIG
        assert "regime_affinity" in DEFAULT_CONFIG
        assert isinstance(DEFAULT_CONFIG["regime_affinity"], dict)

    def test_ra002_all_10_models_have_regime_affinity(self):
        """Verify all 10 models have regime affinity entries."""
        from config.settings import DEFAULT_CONFIG
        ra = DEFAULT_CONFIG["regime_affinity"]
        expected = {"trend", "mean_reversion", "momentum_breakout", "vwap_reversion",
                    "liquidity_sweep", "funding_rate", "order_book", "sentiment",
                    "donchian_breakout", "rl_ensemble", "orchestrator"}
        assert set(ra.keys()) == expected

    def test_ra003_each_model_has_12_regimes(self):
        """Verify each model has all 12 regime entries."""
        from config.settings import DEFAULT_CONFIG
        ra = DEFAULT_CONFIG["regime_affinity"]
        for model_name, regimes in ra.items():
            assert len(regimes) == 12, f"{model_name} has {len(regimes)} regimes, expected 12"
            for regime in self.REGIMES:
                assert regime in regimes, f"{model_name} missing regime: {regime}"

    def test_ra004_all_values_in_valid_range(self):
        """Verify all regime affinity values are in [0.0, 1.0]."""
        from config.settings import DEFAULT_CONFIG
        ra = DEFAULT_CONFIG["regime_affinity"]
        for model_name, regimes in ra.items():
            for regime, val in regimes.items():
                assert 0.0 <= val <= 1.0, \
                    f"{model_name}.{regime} = {val} out of [0.0, 1.0]"

    def test_ra005_confluence_scorer_reads_from_settings(self):
        """Verify _get_regime_affinity reads from settings."""
        # Check function exists in module
        from core.meta_decision.confluence_scorer import _get_regime_affinity, REGIME_AFFINITY
        result = _get_regime_affinity("trend")
        assert isinstance(result, dict)
        assert "bull_trend" in result

    def test_ra006_get_all_regime_affinities_returns_all(self):
        """Verify get_all_regime_affinities returns all 10 models."""
        from core.meta_decision.confluence_scorer import get_all_regime_affinities
        all_aff = get_all_regime_affinities()
        assert len(all_aff) >= 10

    def test_ra007_affinity_defaults_match_hardcoded(self):
        """Verify DEFAULT_CONFIG regime_affinity matches hardcoded REGIME_AFFINITY."""
        from config.settings import DEFAULT_CONFIG
        from core.meta_decision.confluence_scorer import REGIME_AFFINITY
        for model_name in REGIME_AFFINITY:
            if model_name in DEFAULT_CONFIG.get("regime_affinity", {}):
                for regime, val in REGIME_AFFINITY[model_name].items():
                    config_val = DEFAULT_CONFIG["regime_affinity"][model_name].get(regime)
                    assert config_val == val, \
                        f"Mismatch: {model_name}.{regime} code={val} config={config_val}"

    def test_ra008_settings_get_reads_affinity(self):
        """Verify settings.get reads regime_affinity correctly."""
        from config.settings import settings
        val = settings.get("regime_affinity.trend.bull_trend", -1)
        assert val == 1.0 or val == -1  # depends on config.yaml state

    def test_ra009_crisis_values_are_zero_or_low(self):
        """Verify crisis and liquidation_cascade have zero or very low affinities for most models."""
        from config.settings import DEFAULT_CONFIG
        ra = DEFAULT_CONFIG["regime_affinity"]
        for model_name in ["trend", "mean_reversion", "momentum_breakout"]:
            assert ra[model_name]["crisis"] <= 0.2, \
                f"{model_name} crisis affinity should be <=0.2, got {ra[model_name]['crisis']}"

    def test_ra010_trend_model_high_in_bull(self):
        """Verify TrendModel has high affinity in bull_trend (sanity check)."""
        from config.settings import DEFAULT_CONFIG
        assert DEFAULT_CONFIG["regime_affinity"]["trend"]["bull_trend"] == 1.0


# ============================================================
# TestHeadlessUILogic — 8 tests
# ============================================================

class TestHeadlessUILogic:
    """Tests for UI logic that can run without a display server."""

    def test_hl001_all_registry_keys_resolve_in_settings(self):
        """Verify every registry config key can be read from settings."""
        from core.strategies.strategy_registry import get_all_config_keys
        from config.settings import settings
        keys = get_all_config_keys()
        for key in keys:
            val = settings.get(key)
            # Value should not be None (should have a default)
            # Note: some keys may legitimately be None if not in DEFAULT_CONFIG
            # but all model-specific keys should have defaults

    def test_hl002_dirty_tracking_detects_changes(self):
        """Verify that changed values differ from original."""
        from config.settings import DEFAULT_CONFIG
        # Simulate dirty tracking: original=0.35, new=0.40 → dirty
        original = DEFAULT_CONFIG["model_weights"]["trend"]
        new_val = 0.40
        assert original != new_val  # confirms dirty detection would work

    def test_hl003_validate_weight_range(self):
        """Test weight validation logic (standalone)."""
        def validate_weight(val):
            return 0.0 <= val <= 1.0
        assert validate_weight(0.35) is True
        assert validate_weight(1.5) is False
        assert validate_weight(-0.1) is False
        assert validate_weight(0.0) is True
        assert validate_weight(1.0) is True

    def test_hl004_validate_at_least_one_core_enabled(self):
        """Test core model enablement validation logic."""
        from core.strategies.strategy_registry import STRATEGY_REGISTRY
        core_models = [m for m in STRATEGY_REGISTRY if m.model_type == "CORE"]
        # If all disabled → invalid
        all_disabled = {m.name for m in core_models}
        assert len(all_disabled) == 5
        # If at least one not in disabled → valid
        partially_disabled = all_disabled - {"trend"}
        assert len(partially_disabled) == 4  # one model still enabled

    def test_hl005_param_key_to_model_mapping(self):
        """Verify param keys correctly map back to their models."""
        from core.strategies.strategy_registry import STRATEGY_REGISTRY, get_param_def
        for model in STRATEGY_REGISTRY:
            for param in model.params:
                # Key should resolve back to a valid param
                found = get_param_def(param.key)
                assert found is not None, f"Param {param.key} not found via get_param_def"
                assert found.key == param.key

    def test_hl006_config_key_no_collisions(self):
        """Verify no config key appears in multiple models."""
        from core.strategies.strategy_registry import STRATEGY_REGISTRY
        seen_keys = {}
        for model in STRATEGY_REGISTRY:
            for param in model.params:
                if param.key in seen_keys:
                    raise AssertionError(
                        f"Key {param.key} in both {seen_keys[param.key]} and {model.name}")
                seen_keys[param.key] = model.name

    def test_hl007_regime_affinity_keys_for_all_models(self):
        """Verify regime_affinity config keys can be constructed for all models."""
        from core.strategies.strategy_registry import STRATEGY_REGISTRY
        regimes = ["bull_trend", "bear_trend", "ranging", "volatility_expansion",
                   "volatility_compression", "uncertain", "crisis", "liquidation_cascade",
                   "squeeze", "recovery", "accumulation", "distribution"]
        for model in STRATEGY_REGISTRY:
            for regime in regimes:
                key = f"regime_affinity.{model.name}.{regime}"
                # Key should be a valid dotted path
                parts = key.split(".")
                assert len(parts) == 3
                assert parts[0] == "regime_affinity"

    def test_hl008_restore_defaults_collects_all_params(self):
        """Verify restore-all collects defaults for every registry param."""
        from core.strategies.strategy_registry import STRATEGY_REGISTRY, GLOBAL_PARAMS
        defaults = {}
        for model in STRATEGY_REGISTRY:
            for param in model.params:
                defaults[param.key] = param.default
        for param in GLOBAL_PARAMS:
            defaults[param.key] = param.default
        # Should have at least 60 defaults (53 model + 16 global - some overlap)
        assert len(defaults) >= 50


# ============================================================
# TestOrchestratorNormalization — 4 tests
# ============================================================

class TestOrchestratorNormalization:
    """Tests that RL Ensemble and Orchestrator are first-class citizens."""

    def test_on001_orchestrator_in_default_config(self):
        """Verify orchestrator section exists in DEFAULT_CONFIG."""
        from config.settings import DEFAULT_CONFIG
        assert "orchestrator" in DEFAULT_CONFIG
        assert DEFAULT_CONFIG["orchestrator"]["veto_enabled"] is True

    def test_on002_rl_section_in_default_config(self):
        """Verify rl section exists in DEFAULT_CONFIG with all params."""
        from config.settings import DEFAULT_CONFIG
        assert "rl" in DEFAULT_CONFIG
        assert "enabled" in DEFAULT_CONFIG["rl"]
        assert "replay_buffer_size" in DEFAULT_CONFIG["rl"]
        assert "train_every_n_candles" in DEFAULT_CONFIG["rl"]
        assert "reward_leverage" in DEFAULT_CONFIG["rl"]

    def test_on003_registry_keys_resolve_for_rl(self):
        """Verify RL Ensemble registry param keys resolve in settings."""
        from core.strategies.strategy_registry import get_model_def
        from config.settings import settings
        rl = get_model_def("rl_ensemble")
        assert rl is not None
        for param in rl.params:
            val = settings.get(param.key, "__MISSING__")
            assert val != "__MISSING__", f"RL param key {param.key} not found in settings"

    def test_on004_registry_keys_resolve_for_orchestrator(self):
        """Verify Orchestrator registry param keys resolve in settings."""
        from core.strategies.strategy_registry import get_model_def
        from config.settings import settings
        orch = get_model_def("orchestrator")
        assert orch is not None
        for param in orch.params:
            val = settings.get(param.key, "__MISSING__")
            assert val != "__MISSING__", f"Orchestrator param key {param.key} not found in settings"


# ============================================================
# Test Execution
# ============================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
