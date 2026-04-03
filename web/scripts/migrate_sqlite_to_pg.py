#!/usr/bin/env python3
# ============================================================
# SQLite → PostgreSQL Migration Tool
#
# Migrates all data from the desktop SQLite database to the
# web PostgreSQL database. Supports dry-run mode for validation.
#
# Usage:
#   python scripts/migrate_sqlite_to_pg.py \
#     --sqlite-path data/nexus_trader.db \
#     --pg-url postgresql://nexus:nexus@localhost:5432/nexustrader \
#     --dry-run | --execute
#
# Safety:
#   - SQLite opened read-only
#   - PostgreSQL within single transaction (rollback on error)
#   - Idempotent: TRUNCATE before INSERT (within transaction)
#   - Dry-run validates transforms without writing
# ============================================================
from __future__ import annotations

import argparse
import json
import logging
import math
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional

import psycopg2
import psycopg2.extras

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
)
logger = logging.getLogger("migrate")

# ── Table ordering (topological for FK satisfaction) ────────
# Tables without foreign key dependencies come first.
# Tables referencing others come after their parents.
TABLE_ORDER = [
    "exchanges",
    "assets",
    "strategies",
    "settings",
    "system_logs",
    "agent_signals",
    "signal_log",
    "ohlcv",
    "features",
    "strategy_metrics",
    "signals",
    "trades",
    "orders",
    "positions",
    "sentiment_data",
    "ml_models",
    "model_predictions",
    "market_regimes",
    "portfolio_snapshots",
    "backtest_results",
    "paper_trades",
    "live_trades",
    "trade_feedback",
    "strategy_tuning_proposals",
    "applied_strategy_changes",
    "tuning_proposal_outcomes",
]

# Tables to skip during migration (too large or not needed in web)
SKIP_TABLES: set[str] = set()

# Batch size for INSERT operations
BATCH_SIZE = 1000

# Columns that should be JSONB in PostgreSQL
JSONB_COLUMNS: dict[str, set[str]] = {
    "signals": {"timeframe_alignment", "indicator_values", "ai_prediction"},
    "trades": {"explanation"},
    "sentiment_data": {"raw_data"},
    "ml_models": {"feature_importance"},
    "market_regimes": {"features"},
    "portfolio_snapshots": {"holdings"},
    "backtest_results": {"run_config", "equity_curve", "trade_log"},
    "paper_trades": {"models_fired"},
    "live_trades": {"models_fired"},
    "system_logs": {"details"},
    "agent_signals": {"payload"},
    "signal_log": {"models_fired"},
    "trade_feedback": {
        "models_fired", "hard_overrides", "root_causes",
        "recommendations", "penalty_log", "decision_outcome_matrix",
    },
    "strategy_tuning_proposals": {"trigger_evidence", "backtest_result"},
    "strategies": {"definition"},
}


def _transform_value(
    table: str, col: str, value: Any, col_type: str,
) -> Any:
    """
    Transform a single SQLite value to PostgreSQL-compatible format.

    Conversions:
      - JSON text → parsed Python dict/list (for JSONB columns)
      - Datetime string → timezone-aware datetime
      - BOOLEAN int (0/1) → Python bool
      - NaN float → None
      - Empty string → None (for nullable columns)
    """
    if value is None:
        return None

    # JSON → JSONB: parse JSON strings
    jsonb_cols = JSONB_COLUMNS.get(table, set())
    if col in jsonb_cols:
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                return json.dumps(parsed)  # Re-serialize for psycopg2 JSONB
            except (json.JSONDecodeError, TypeError):
                return None
        elif isinstance(value, (dict, list)):
            return json.dumps(value)
        return None

    # Float NaN → None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None

    # Datetime string → timezone-aware
    if isinstance(value, str) and col_type.upper() in ("DATETIME", "TIMESTAMP"):
        try:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            return value

    # Boolean integer → Python bool
    if col_type.upper() == "BOOLEAN" and isinstance(value, int):
        return bool(value)

    return value


def _get_sqlite_tables(conn: sqlite3.Connection) -> list[str]:
    """Get list of tables in SQLite database."""
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    return [row[0] for row in cursor.fetchall()]


def _get_sqlite_columns(
    conn: sqlite3.Connection, table: str,
) -> list[tuple[str, str]]:
    """Get (column_name, column_type) pairs for a table."""
    cursor = conn.execute(f"PRAGMA table_info({table})")
    return [(row[1], row[2]) for row in cursor.fetchall()]


def _get_pg_tables(conn) -> set[str]:
    """Get set of table names in PostgreSQL."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public'"
    )
    return {row[0] for row in cursor.fetchall()}


def _get_pg_columns(conn, table: str) -> set[str]:
    """Get set of column names for a PostgreSQL table."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s",
        (table,),
    )
    return {row[0] for row in cursor.fetchall()}


def _reset_sequence(pg_conn, table: str, pk_col: str = "id"):
    """Reset auto-increment sequence to max(id)+1."""
    cursor = pg_conn.cursor()
    try:
        cursor.execute(
            f"SELECT setval(pg_get_serial_sequence('{table}', '{pk_col}'), "
            f"COALESCE(MAX({pk_col}), 0) + 1, false) FROM {table}"
        )
    except Exception:
        pass  # Table may not have a serial/sequence


def migrate(
    sqlite_path: str,
    pg_url: str,
    dry_run: bool = True,
    skip_system_logs: bool = False,
) -> dict[str, Any]:
    """
    Migrate data from SQLite to PostgreSQL.

    Returns a report dict with counts and errors.
    """
    report: dict[str, Any] = {
        "mode": "dry_run" if dry_run else "execute",
        "sqlite_path": sqlite_path,
        "tables": {},
        "errors": [],
        "total_rows_migrated": 0,
        "duration_seconds": 0,
    }

    start_time = time.time()

    if skip_system_logs:
        SKIP_TABLES.add("system_logs")

    # Connect to SQLite (read-only)
    sqlite_uri = f"file:{sqlite_path}?mode=ro"
    try:
        sqlite_conn = sqlite3.connect(sqlite_uri, uri=True)
        sqlite_conn.row_factory = sqlite3.Row
    except Exception as e:
        report["errors"].append(f"Failed to connect to SQLite: {e}")
        return report

    # Connect to PostgreSQL
    try:
        pg_conn = psycopg2.connect(pg_url)
        pg_conn.autocommit = False  # Single transaction
    except Exception as e:
        report["errors"].append(f"Failed to connect to PostgreSQL: {e}")
        sqlite_conn.close()
        return report

    sqlite_tables = set(_get_sqlite_tables(sqlite_conn))
    pg_tables = _get_pg_tables(pg_conn)

    logger.info("SQLite tables: %d, PostgreSQL tables: %d", len(sqlite_tables), len(pg_tables))

    try:
        cursor = pg_conn.cursor()

        for table in TABLE_ORDER:
            if table in SKIP_TABLES:
                logger.info("SKIP: %s (in skip list)", table)
                report["tables"][table] = {"status": "skipped", "rows": 0}
                continue

            if table not in sqlite_tables:
                logger.info("SKIP: %s (not in SQLite)", table)
                report["tables"][table] = {"status": "not_in_sqlite", "rows": 0}
                continue

            if table not in pg_tables:
                logger.warning("SKIP: %s (not in PostgreSQL)", table)
                report["tables"][table] = {"status": "not_in_pg", "rows": 0}
                report["errors"].append(f"Table {table} exists in SQLite but not PostgreSQL")
                continue

            # Get column info
            sqlite_cols = _get_sqlite_columns(sqlite_conn, table)
            pg_cols = _get_pg_columns(pg_conn, table)

            # Only migrate columns that exist in both
            common_cols = [(name, ctype) for name, ctype in sqlite_cols if name in pg_cols]
            col_names = [c[0] for c in common_cols]
            col_types = {c[0]: c[1] for c in common_cols}

            if not col_names:
                logger.warning("SKIP: %s (no common columns)", table)
                report["tables"][table] = {"status": "no_common_cols", "rows": 0}
                continue

            # Count SQLite rows
            count_cursor = sqlite_conn.execute(f"SELECT COUNT(*) FROM {table}")
            sqlite_count = count_cursor.fetchone()[0]
            logger.info("TABLE: %s — %d rows, %d columns", table, sqlite_count, len(col_names))

            if dry_run:
                # In dry-run, validate a sample of transforms
                sample_cursor = sqlite_conn.execute(
                    f"SELECT {','.join(col_names)} FROM {table} LIMIT 100"
                )
                sample_rows = sample_cursor.fetchall()
                transform_errors = 0
                for row in sample_rows:
                    for i, col in enumerate(col_names):
                        try:
                            _transform_value(table, col, row[i], col_types[col])
                        except Exception as e:
                            transform_errors += 1
                            if transform_errors <= 3:
                                logger.warning(
                                    "Transform error in %s.%s: %s (value=%r)",
                                    table, col, e, row[i],
                                )

                report["tables"][table] = {
                    "status": "validated",
                    "rows": sqlite_count,
                    "columns": len(col_names),
                    "transform_errors": transform_errors,
                }
                report["total_rows_migrated"] += sqlite_count
                continue

            # Execute mode: TRUNCATE + batch INSERT
            cursor.execute(f"TRUNCATE {table} CASCADE")
            logger.info("  TRUNCATED %s", table)

            # Read all rows from SQLite
            read_cursor = sqlite_conn.execute(
                f"SELECT {','.join(col_names)} FROM {table}"
            )

            migrated = 0
            batch = []

            for row in read_cursor:
                transformed = []
                for i, col in enumerate(col_names):
                    val = _transform_value(table, col, row[i], col_types[col])
                    transformed.append(val)
                batch.append(tuple(transformed))

                if len(batch) >= BATCH_SIZE:
                    placeholders = ",".join(["%s"] * len(col_names))
                    cols_str = ",".join(col_names)
                    psycopg2.extras.execute_batch(
                        cursor,
                        f"INSERT INTO {table} ({cols_str}) VALUES ({placeholders})",
                        batch,
                    )
                    migrated += len(batch)
                    batch = []

            # Flush remaining
            if batch:
                placeholders = ",".join(["%s"] * len(col_names))
                cols_str = ",".join(col_names)
                psycopg2.extras.execute_batch(
                    cursor,
                    f"INSERT INTO {table} ({cols_str}) VALUES ({placeholders})",
                    batch,
                )
                migrated += len(batch)

            # Reset sequence
            if "id" in col_names:
                _reset_sequence(pg_conn, table, "id")

            logger.info("  MIGRATED %s: %d rows", table, migrated)
            report["tables"][table] = {
                "status": "migrated",
                "rows": migrated,
                "columns": len(col_names),
            }
            report["total_rows_migrated"] += migrated

        if not dry_run:
            pg_conn.commit()
            logger.info("Transaction COMMITTED")
        else:
            logger.info("Dry-run complete — no changes written")

    except Exception as e:
        logger.error("Migration FAILED: %s", e, exc_info=True)
        report["errors"].append(f"Migration failed: {e}")
        if not dry_run:
            pg_conn.rollback()
            logger.info("Transaction ROLLED BACK")
    finally:
        sqlite_conn.close()
        pg_conn.close()

    report["duration_seconds"] = round(time.time() - start_time, 2)
    return report


def main():
    parser = argparse.ArgumentParser(
        description="Migrate NexusTrader data from SQLite to PostgreSQL",
    )
    parser.add_argument(
        "--sqlite-path", required=True,
        help="Path to SQLite database file",
    )
    parser.add_argument(
        "--pg-url", required=True,
        help="PostgreSQL connection URL",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--dry-run", action="store_true",
        help="Validate transforms without writing to PostgreSQL",
    )
    group.add_argument(
        "--execute", action="store_true",
        help="Execute the migration (writes to PostgreSQL)",
    )
    parser.add_argument(
        "--skip-system-logs", action="store_true",
        help="Skip system_logs table (can be very large)",
    )
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("NexusTrader SQLite → PostgreSQL Migration")
    logger.info("Mode: %s", "DRY RUN" if args.dry_run else "EXECUTE")
    logger.info("SQLite: %s", args.sqlite_path)
    logger.info("PostgreSQL: %s", args.pg_url.split("@")[-1] if "@" in args.pg_url else "***")
    logger.info("=" * 60)

    report = migrate(
        sqlite_path=args.sqlite_path,
        pg_url=args.pg_url,
        dry_run=args.dry_run,
        skip_system_logs=args.skip_system_logs,
    )

    # Print report
    print("\n" + "=" * 60)
    print("MIGRATION REPORT")
    print("=" * 60)
    print(f"Mode:          {report['mode']}")
    print(f"Duration:      {report['duration_seconds']}s")
    print(f"Total rows:    {report['total_rows_migrated']}")
    print(f"Errors:        {len(report['errors'])}")
    print()

    for table, info in report["tables"].items():
        status = info["status"]
        rows = info["rows"]
        icon = "✅" if status in ("migrated", "validated") else "⏭️" if "skip" in status else "❌"
        print(f"  {icon} {table:40s} {status:15s} {rows:>8d} rows")

    if report["errors"]:
        print("\nERRORS:")
        for err in report["errors"]:
            print(f"  ❌ {err}")

    # Write report to JSON
    report_path = "migration_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\nFull report: {report_path}")

    sys.exit(1 if report["errors"] else 0)


if __name__ == "__main__":
    main()
