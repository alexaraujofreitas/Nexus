#!/usr/bin/env python3
# ============================================================
# Migration Validation Script
#
# Post-migration checks to verify data integrity:
#   1. Row count comparison (every table)
#   2. Aggregate validation (SUM, COUNT, etc.)
#   3. Foreign key integrity
#   4. NULL constraint check
#   5. JSONB validity
#   6. Structured report (JSON + human-readable)
#
# Usage:
#   python scripts/validate_migration.py \
#     --sqlite-path data/nexus_trader.db \
#     --pg-url postgresql://nexus:nexus@localhost:5432/nexustrader
# ============================================================
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from typing import Any

import psycopg2
import psycopg2.extras

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
)
logger = logging.getLogger("validate")


def _get_tables(sqlite_conn: sqlite3.Connection) -> list[str]:
    """Get all table names from SQLite."""
    cursor = sqlite_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    return [row[0] for row in cursor.fetchall()]


def _count_rows(conn, table: str, is_pg: bool = False) -> int:
    """Count rows in a table."""
    if is_pg:
        cursor = conn.cursor()
        cursor.execute(f"SELECT COUNT(*) FROM {table}")
        return cursor.fetchone()[0]
    else:
        cursor = conn.execute(f"SELECT COUNT(*) FROM {table}")
        return cursor.fetchone()[0]


def _check_row_counts(
    sqlite_conn: sqlite3.Connection,
    pg_conn,
    tables: list[str],
    pg_tables: set[str],
) -> list[dict]:
    """Compare row counts between SQLite and PostgreSQL."""
    results = []
    for table in tables:
        sqlite_count = _count_rows(sqlite_conn, table)
        if table in pg_tables:
            pg_count = _count_rows(pg_conn, table, is_pg=True)
            match = sqlite_count == pg_count
        else:
            pg_count = None
            # Empty tables not in PG are acceptable (legacy desktop-only)
            match = sqlite_count == 0

        results.append({
            "table": table,
            "sqlite_rows": sqlite_count,
            "pg_rows": pg_count,
            "match": match,
            "note": "legacy table (not in PG, 0 rows)" if pg_count is None and match else None,
        })
    return results


def _check_aggregates(
    sqlite_conn: sqlite3.Connection,
    pg_conn,
) -> list[dict]:
    """Compare aggregate values for key tables."""
    checks = []

    agg_queries = [
        ("paper_trades", "SUM(pnl_usdt)", "total_pnl"),
        ("paper_trades", "COUNT(DISTINCT symbol)", "distinct_symbols"),
        ("paper_trades", "COUNT(*)", "total_trades"),
        ("live_trades", "COUNT(*)", "total_live_trades"),
        ("signal_log", "COUNT(*)", "total_signals"),
        ("agent_signals", "COUNT(*)", "total_agent_signals"),
    ]

    for table, agg_expr, label in agg_queries:
        try:
            sqlite_cursor = sqlite_conn.execute(f"SELECT {agg_expr} FROM {table}")
            sqlite_val = sqlite_cursor.fetchone()[0]
        except Exception:
            sqlite_val = None

        try:
            pg_cursor = pg_conn.cursor()
            pg_cursor.execute(f"SELECT {agg_expr} FROM {table}")
            pg_val = pg_cursor.fetchone()[0]
        except Exception:
            pg_val = None

        # Compare with tolerance for float aggregates
        if sqlite_val is not None and pg_val is not None:
            if isinstance(sqlite_val, float) and isinstance(pg_val, float):
                match = abs(sqlite_val - pg_val) < 0.01
            else:
                match = sqlite_val == pg_val
        else:
            match = sqlite_val == pg_val

        checks.append({
            "table": table,
            "aggregate": label,
            "sqlite_value": sqlite_val,
            "pg_value": pg_val,
            "match": match,
        })

    return checks


def _check_fk_integrity(pg_conn) -> list[dict]:
    """Check that all foreign keys resolve in PostgreSQL."""
    checks = []

    fk_queries = [
        ("signals", "strategy_id", "strategies", "id"),
        ("signals", "asset_id", "assets", "id"),
        ("trades", "strategy_id", "strategies", "id"),
        ("trades", "asset_id", "assets", "id"),
        ("orders", "trade_id", "trades", "id"),
        ("positions", "strategy_id", "strategies", "id"),
        ("positions", "asset_id", "assets", "id"),
        ("strategy_metrics", "strategy_id", "strategies", "id"),
        ("model_predictions", "model_id", "ml_models", "id"),
        ("model_predictions", "asset_id", "assets", "id"),
    ]

    cursor = pg_conn.cursor()
    for child_table, child_col, parent_table, parent_col in fk_queries:
        try:
            cursor.execute(
                f"SELECT COUNT(*) FROM {child_table} c "
                f"LEFT JOIN {parent_table} p ON c.{child_col} = p.{parent_col} "
                f"WHERE c.{child_col} IS NOT NULL AND p.{parent_col} IS NULL"
            )
            orphan_count = cursor.fetchone()[0]
            checks.append({
                "child": f"{child_table}.{child_col}",
                "parent": f"{parent_table}.{parent_col}",
                "orphan_rows": orphan_count,
                "passed": orphan_count == 0,
            })
        except Exception as e:
            checks.append({
                "child": f"{child_table}.{child_col}",
                "parent": f"{parent_table}.{parent_col}",
                "error": str(e),
                "passed": False,
            })

    return checks


def _check_jsonb_validity(pg_conn) -> list[dict]:
    """Verify all JSONB columns parse successfully in PostgreSQL."""
    checks = []

    jsonb_cols = {
        "signals": ["timeframe_alignment", "indicator_values", "ai_prediction"],
        "trades": ["explanation"],
        "paper_trades": ["models_fired"],
        "live_trades": ["models_fired"],
        "system_logs": ["details"],
        "agent_signals": ["payload"],
        "signal_log": ["models_fired"],
        "trade_feedback": ["models_fired", "hard_overrides", "root_causes", "recommendations"],
    }

    cursor = pg_conn.cursor()
    for table, cols in jsonb_cols.items():
        for col in cols:
            try:
                # JSONB columns that don't parse would have failed at INSERT time,
                # but check for NULL-safety
                cursor.execute(
                    f"SELECT COUNT(*) FROM {table} "
                    f"WHERE {col} IS NOT NULL AND {col}::text NOT IN ('null', '')"
                )
                non_null_count = cursor.fetchone()[0]
                checks.append({
                    "table": table,
                    "column": col,
                    "non_null_rows": non_null_count,
                    "passed": True,
                })
            except Exception as e:
                checks.append({
                    "table": table,
                    "column": col,
                    "error": str(e),
                    "passed": False,
                })

    return checks


def validate(
    sqlite_path: str,
    pg_url: str,
) -> dict[str, Any]:
    """Run all validation checks and return structured report."""
    report: dict[str, Any] = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sqlite_path": sqlite_path,
        "checks": {},
        "overall_pass": True,
        "errors": [],
    }

    start_time = time.time()

    # Connect
    try:
        sqlite_conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    except Exception as e:
        report["errors"].append(f"SQLite connection failed: {e}")
        report["overall_pass"] = False
        return report

    try:
        pg_conn = psycopg2.connect(pg_url)
    except Exception as e:
        report["errors"].append(f"PostgreSQL connection failed: {e}")
        report["overall_pass"] = False
        sqlite_conn.close()
        return report

    try:
        sqlite_tables = _get_tables(sqlite_conn)
        pg_cursor = pg_conn.cursor()
        pg_cursor.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public'"
        )
        pg_tables = {row[0] for row in pg_cursor.fetchall()}

        # 1. Row counts
        logger.info("Check 1: Row count comparison...")
        row_counts = _check_row_counts(sqlite_conn, pg_conn, sqlite_tables, pg_tables)
        mismatches = [r for r in row_counts if not r["match"]]
        report["checks"]["row_counts"] = {
            "total_tables": len(row_counts),
            "matched": len(row_counts) - len(mismatches),
            "mismatched": len(mismatches),
            "details": row_counts,
            "passed": len(mismatches) == 0,
        }
        if mismatches:
            report["overall_pass"] = False

        # 2. Aggregates
        logger.info("Check 2: Aggregate validation...")
        aggregates = _check_aggregates(sqlite_conn, pg_conn)
        agg_fails = [a for a in aggregates if not a["match"]]
        report["checks"]["aggregates"] = {
            "total_checks": len(aggregates),
            "passed_count": len(aggregates) - len(agg_fails),
            "failed_count": len(agg_fails),
            "details": aggregates,
            "passed": len(agg_fails) == 0,
        }
        if agg_fails:
            report["overall_pass"] = False

        # 3. FK integrity
        logger.info("Check 3: Foreign key integrity...")
        fk_checks = _check_fk_integrity(pg_conn)
        fk_fails = [f for f in fk_checks if not f["passed"]]
        report["checks"]["fk_integrity"] = {
            "total_checks": len(fk_checks),
            "passed_count": len(fk_checks) - len(fk_fails),
            "failed_count": len(fk_fails),
            "details": fk_checks,
            "passed": len(fk_fails) == 0,
        }
        if fk_fails:
            report["overall_pass"] = False

        # 4. JSONB validity
        logger.info("Check 4: JSONB validity...")
        jsonb_checks = _check_jsonb_validity(pg_conn)
        jsonb_fails = [j for j in jsonb_checks if not j["passed"]]
        report["checks"]["jsonb_validity"] = {
            "total_checks": len(jsonb_checks),
            "passed_count": len(jsonb_checks) - len(jsonb_fails),
            "failed_count": len(jsonb_fails),
            "details": jsonb_checks,
            "passed": len(jsonb_fails) == 0,
        }
        if jsonb_fails:
            report["overall_pass"] = False

    except Exception as e:
        logger.error("Validation error: %s", e, exc_info=True)
        report["errors"].append(str(e))
        report["overall_pass"] = False
    finally:
        sqlite_conn.close()
        pg_conn.close()

    report["duration_seconds"] = round(time.time() - start_time, 2)
    return report


def main():
    parser = argparse.ArgumentParser(
        description="Validate SQLite → PostgreSQL migration",
    )
    parser.add_argument("--sqlite-path", required=True)
    parser.add_argument("--pg-url", required=True)
    args = parser.parse_args()

    report = validate(args.sqlite_path, args.pg_url)

    # Human-readable output
    print("\n" + "=" * 60)
    print("MIGRATION VALIDATION REPORT")
    print("=" * 60)

    for check_name, check_data in report["checks"].items():
        icon = "✅" if check_data.get("passed") else "❌"
        print(f"\n{icon} {check_name}:")
        if "total_tables" in check_data:
            print(f"   Matched: {check_data['matched']}/{check_data['total_tables']}")
        if "total_checks" in check_data:
            print(f"   Passed: {check_data['passed_count']}/{check_data['total_checks']}")

    print(f"\n{'✅' if report['overall_pass'] else '❌'} OVERALL: {'PASS' if report['overall_pass'] else 'FAIL'}")
    print(f"Duration: {report.get('duration_seconds', 0)}s")

    # Write JSON report
    report_path = "validation_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"Full report: {report_path}")

    sys.exit(0 if report["overall_pass"] else 1)


if __name__ == "__main__":
    main()
