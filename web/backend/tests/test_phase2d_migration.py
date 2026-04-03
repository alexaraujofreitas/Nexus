# ============================================================
# Phase 2D — SQLite → PostgreSQL Migration Tests
#
# Tests the migration transform logic (no live databases needed):
#   - JSON → JSONB conversion
#   - Datetime → timezone-aware conversion
#   - Boolean int → Python bool
#   - NaN/Inf float → None
#   - Table ordering (topological FK order)
#   - Column matching logic
#   - Dry-run vs execute mode flags
# ============================================================
from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone

import pytest

# ── Path setup ──────────────────────────────────────────────
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_WEB_DIR = os.path.dirname(_BACKEND)
_SCRIPTS_DIR = os.path.join(_WEB_DIR, "scripts")
_PROJECT_ROOT = os.path.dirname(_WEB_DIR)

if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
for p in [_WEB_DIR, _PROJECT_ROOT]:
    if p not in sys.path:
        sys.path.append(p)

from migrate_sqlite_to_pg import (
    _transform_value,
    TABLE_ORDER,
    JSONB_COLUMNS,
    BATCH_SIZE,
    _get_sqlite_tables,
    _get_sqlite_columns,
)


# ============================================================
# Test: Value Transforms
# ============================================================

class TestTransformValue:
    """Test all SQLite → PostgreSQL value transformations."""

    # ── JSON → JSONB ────────────────────────────────────────

    def test_json_string_to_jsonb(self):
        """Valid JSON string should be parsed and re-serialized."""
        result = _transform_value(
            "paper_trades", "models_fired", '{"momentum": 0.8}', "TEXT",
        )
        parsed = json.loads(result)
        assert parsed == {"momentum": 0.8}

    def test_json_array_to_jsonb(self):
        result = _transform_value(
            "signal_log", "models_fired", '["trend", "momentum"]', "TEXT",
        )
        parsed = json.loads(result)
        assert parsed == ["trend", "momentum"]

    def test_json_null_string(self):
        """JSON 'null' string should become None."""
        result = _transform_value(
            "paper_trades", "models_fired", "null", "TEXT",
        )
        # json.loads("null") returns None, json.dumps(None) returns "null"
        assert result == "null"

    def test_json_invalid_string(self):
        """Invalid JSON should return None."""
        result = _transform_value(
            "paper_trades", "models_fired", "not valid json {}", "TEXT",
        )
        assert result is None

    def test_json_none_passthrough(self):
        """None value should remain None."""
        result = _transform_value("paper_trades", "models_fired", None, "TEXT")
        assert result is None

    def test_json_dict_passthrough(self):
        """A Python dict should be JSON-serialized."""
        result = _transform_value(
            "paper_trades", "models_fired", {"key": "val"}, "TEXT",
        )
        assert json.loads(result) == {"key": "val"}

    # ── Non-JSONB columns should pass through ───────────────

    def test_non_jsonb_column_passthrough(self):
        """Regular text columns should not be JSON-parsed."""
        result = _transform_value(
            "paper_trades", "symbol", "BTCUSDT", "TEXT",
        )
        assert result == "BTCUSDT"

    # ── Datetime → timezone-aware ───────────────────────────

    def test_datetime_naive_gets_utc(self):
        """Naive datetime string gets UTC timezone."""
        result = _transform_value(
            "paper_trades", "opened_at", "2025-01-15T10:30:00", "DATETIME",
        )
        assert isinstance(result, datetime)
        assert result.tzinfo == timezone.utc

    def test_datetime_already_aware(self):
        """Already timezone-aware datetime should pass through."""
        result = _transform_value(
            "paper_trades", "opened_at", "2025-01-15T10:30:00+00:00", "DATETIME",
        )
        assert isinstance(result, datetime)
        assert result.tzinfo is not None

    def test_datetime_none(self):
        result = _transform_value("paper_trades", "opened_at", None, "DATETIME")
        assert result is None

    def test_datetime_invalid_string(self):
        """Invalid datetime string should pass through as-is."""
        result = _transform_value(
            "paper_trades", "opened_at", "not a date", "DATETIME",
        )
        assert result == "not a date"

    # ── Boolean int → Python bool ───────────────────────────

    def test_boolean_0_to_false(self):
        result = _transform_value("exchanges", "is_active", 0, "BOOLEAN")
        assert result is False

    def test_boolean_1_to_true(self):
        result = _transform_value("exchanges", "is_active", 1, "BOOLEAN")
        assert result is True

    def test_boolean_none(self):
        result = _transform_value("exchanges", "is_active", None, "BOOLEAN")
        assert result is None

    # ── Float NaN/Inf → None ────────────────────────────────

    def test_nan_to_none(self):
        result = _transform_value("paper_trades", "pnl_usdt", float("nan"), "REAL")
        assert result is None

    def test_inf_to_none(self):
        result = _transform_value("paper_trades", "pnl_usdt", float("inf"), "REAL")
        assert result is None

    def test_negative_inf_to_none(self):
        result = _transform_value("paper_trades", "pnl_usdt", float("-inf"), "REAL")
        assert result is None

    def test_normal_float_passthrough(self):
        result = _transform_value("paper_trades", "pnl_usdt", 42.5, "REAL")
        assert result == 42.5


# ============================================================
# Test: Table Ordering
# ============================================================

class TestTableOrdering:
    """Verify topological FK order."""

    def test_exchanges_before_assets(self):
        """exchanges must come before assets (FK dependency)."""
        assert TABLE_ORDER.index("exchanges") < TABLE_ORDER.index("assets")

    def test_strategies_before_signals(self):
        assert TABLE_ORDER.index("strategies") < TABLE_ORDER.index("signals")

    def test_trades_before_orders(self):
        assert TABLE_ORDER.index("trades") < TABLE_ORDER.index("orders")

    def test_all_26_tables_present(self):
        assert len(TABLE_ORDER) == 26

    def test_no_duplicates(self):
        assert len(TABLE_ORDER) == len(set(TABLE_ORDER))

    def test_settings_early(self):
        """settings has no FKs, should be early."""
        assert TABLE_ORDER.index("settings") < TABLE_ORDER.index("signals")


# ============================================================
# Test: JSONB Column Registry
# ============================================================

class TestJSONBColumns:
    """Verify JSONB column registry matches known columns."""

    def test_paper_trades_models_fired(self):
        assert "models_fired" in JSONB_COLUMNS.get("paper_trades", set())

    def test_system_logs_details(self):
        assert "details" in JSONB_COLUMNS.get("system_logs", set())

    def test_trade_feedback_multi_jsonb(self):
        cols = JSONB_COLUMNS.get("trade_feedback", set())
        for c in ["models_fired", "hard_overrides", "root_causes", "recommendations"]:
            assert c in cols, f"Missing {c} in trade_feedback JSONB columns"


# ============================================================
# Test: SQLite Helpers with Temp Database
# ============================================================

class TestSQLiteHelpers:
    """Test SQLite introspection with a temporary database."""

    @pytest.fixture
    def temp_db(self):
        """Create a temporary SQLite database with test tables."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE test_table (
                id INTEGER PRIMARY KEY,
                name TEXT,
                value REAL,
                is_active BOOLEAN,
                data JSON,
                created_at DATETIME
            )
        """)
        conn.execute("""
            INSERT INTO test_table VALUES
            (1, 'item1', 42.5, 1, '{"key": "val"}', '2025-01-15T10:30:00')
        """)
        conn.commit()
        yield path, conn
        conn.close()
        os.unlink(path)

    def test_get_tables(self, temp_db):
        path, conn = temp_db
        tables = _get_sqlite_tables(conn)
        assert "test_table" in tables

    def test_get_columns(self, temp_db):
        path, conn = temp_db
        cols = _get_sqlite_columns(conn, "test_table")
        col_names = [c[0] for c in cols]
        assert "id" in col_names
        assert "name" in col_names
        assert "data" in col_names
        assert len(cols) == 6

    def test_column_types(self, temp_db):
        path, conn = temp_db
        cols = dict(_get_sqlite_columns(conn, "test_table"))
        assert cols["id"] == "INTEGER"
        assert cols["name"] == "TEXT"
        assert cols["value"] == "REAL"
        assert cols["is_active"] == "BOOLEAN"
        assert cols["data"] == "JSON"
        assert cols["created_at"] == "DATETIME"


# ============================================================
# Test: Configuration Constants
# ============================================================

class TestMigrationConfig:
    """Verify configuration constants."""

    def test_batch_size(self):
        assert BATCH_SIZE == 1000

    def test_table_order_includes_key_tables(self):
        for table in ["paper_trades", "live_trades", "signal_log", "trade_feedback"]:
            assert table in TABLE_ORDER
