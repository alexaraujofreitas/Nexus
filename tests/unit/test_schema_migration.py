# ============================================================
# Phase 2 Tests — Schema Migration (Audit Finding 1)
# 2 tests per PHASE1E_TEST_PLAN.md
# ============================================================
import pytest
from sqlalchemy import create_engine, text, inspect as sa_inspect
from sqlalchemy.pool import StaticPool


# The 8 intraday columns that must exist after migration
INTRADAY_COLUMNS = [
    "strategy_class",
    "tqs_score",
    "capital_weight",
    "signal_age_ms",
    "setup_bar_ts",
    "trigger_bar_ts",
    "gtf_passed",
    "execution_quality_score",
]


class TestSchemaMigration:

    def test_migrate_schema_has_all_8_intraday_columns(self, monkeypatch):
        """
        _migrate_schema() adds all 8 intraday columns to paper_trades table.
        Verifies by running against an in-memory DB with the paper_trades table
        already created (simulating an existing DB before the intraday redesign).
        """
        from sqlalchemy.orm import sessionmaker
        from core.database.engine import Base, _migrate_schema
        import core.database.engine as db_module
        import core.database.models  # noqa: register models

        # Create in-memory engine
        test_engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

        # Create tables (simulates existing DB)
        Base.metadata.create_all(test_engine)

        # Patch the module-level engine so _migrate_schema() uses our test engine
        monkeypatch.setattr(db_module, "engine", test_engine)

        # Run migration
        _migrate_schema()

        # Verify all 8 columns exist in paper_trades
        inspector = sa_inspect(test_engine)
        columns = {col["name"] for col in inspector.get_columns("paper_trades")}

        for col in INTRADAY_COLUMNS:
            assert col in columns, f"Missing intraday column: {col}"

        test_engine.dispose()

    def test_existing_db_opens_after_migration(self, monkeypatch):
        """
        An existing DB (with paper_trades already populated) can be opened
        and queried after _migrate_schema() adds the new columns.

        Uses a raw SQL table definition to simulate a DB created before the
        intraday redesign (no strategy_class etc. columns).
        """
        from core.database.engine import _migrate_schema
        import core.database.engine as db_module

        test_engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

        # Create a bare-bones paper_trades table (simulating pre-Phase2 DB)
        with test_engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE paper_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol VARCHAR(30) NOT NULL,
                    side VARCHAR(5) NOT NULL,
                    regime VARCHAR(40) DEFAULT '',
                    timeframe VARCHAR(10) DEFAULT '',
                    entry_price REAL NOT NULL,
                    exit_price REAL NOT NULL,
                    size_usdt REAL NOT NULL,
                    pnl_usdt REAL NOT NULL,
                    pnl_pct REAL NOT NULL,
                    score REAL DEFAULT 0.0,
                    exit_reason VARCHAR(30) DEFAULT '',
                    duration_s INTEGER DEFAULT 0,
                    opened_at VARCHAR(40) NOT NULL,
                    closed_at VARCHAR(40) NOT NULL,
                    created_at DATETIME
                )
            """))
            conn.execute(text(
                "INSERT INTO paper_trades "
                "(symbol, side, entry_price, exit_price, size_usdt, pnl_usdt, "
                " pnl_pct, opened_at, closed_at) "
                "VALUES ('BTC/USDT', 'buy', 65000.0, 66000.0, 100.0, 1.54, "
                " 1.54, '2026-04-06T12:00:00', '2026-04-06T13:00:00')"
            ))
            conn.commit()

        # Run migration against this pre-existing DB
        monkeypatch.setattr(db_module, "engine", test_engine)
        _migrate_schema()

        # Verify the pre-existing row is still accessible and new columns are NULL
        with test_engine.connect() as conn:
            result = conn.execute(text(
                "SELECT symbol, strategy_class, tqs_score FROM paper_trades LIMIT 1"
            )).fetchone()
            assert result[0] == "BTC/USDT"
            assert result[1] is None  # new column defaults to NULL
            assert result[2] is None

        test_engine.dispose()
