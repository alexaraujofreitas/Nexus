# ============================================================
# NEXUS TRADER — Database Engine
# SQLAlchemy 2.0 setup with SQLite
# ============================================================

import logging
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker, Session, DeclarativeBase
from sqlalchemy.pool import StaticPool
from contextlib import contextmanager
from config.constants import DB_PATH

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """SQLAlchemy declarative base for all models."""
    pass


# ── Engine Configuration ──────────────────────────────────────
def _create_db_engine():
    engine = create_engine(
        f"sqlite:///{DB_PATH}",
        connect_args={
            "check_same_thread": False,
            "timeout": 30,
        },
        poolclass=StaticPool,
        echo=False,
    )

    # Enable WAL mode and foreign keys on every connection
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        # Each PRAGMA is wrapped individually.  On network/VM mounts (NFS,
        # FUSE, Windows shares via the Cowork sandbox) some PRAGMAs that
        # require writing to the DB can raise "disk I/O error" if the file is
        # simultaneously open by another process (e.g. NexusTrader on the host
        # while the VM runs UI tests).  Non-fatal: skip PRAGMAs that fail.
        _pragmas = [
            ("PRAGMA journal_mode=WAL",        "PRAGMA journal_mode=DELETE"),
            ("PRAGMA foreign_keys=ON",          None),
            ("PRAGMA synchronous=NORMAL",       None),
            ("PRAGMA cache_size=10000",         None),
            ("PRAGMA temp_store=MEMORY",        None),
        ]
        for primary, fallback in _pragmas:
            try:
                cursor.execute(primary)
            except Exception:
                if fallback:
                    try:
                        cursor.execute(fallback)
                    except Exception:
                        pass
        cursor.close()

    return engine


engine = _create_db_engine()
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def init_database():
    """Create all tables and run initial setup."""
    from core.database import models  # noqa: F401 — ensures models are registered
    Base.metadata.create_all(engine)
    _migrate_schema()
    _fix_ai_strategy_types()
    _seed_default_settings()
    logger.info("Database initialized at: %s", DB_PATH)


def _migrate_schema():
    """
    Apply lightweight ALTER TABLE migrations for columns added after initial schema creation.
    SQLAlchemy create_all() does NOT add columns to existing tables, so we handle it here.
    """
    migrations = [
        # (table, column, SQL type + default)
        ("trading_rules", "status",         "VARCHAR(20) DEFAULT 'draft'"),
        ("trading_rules", "runs_completed", "INTEGER DEFAULT 0"),
        ("exchanges",     "demo_mode",      "BOOLEAN DEFAULT 0"),
        # Session 30: position-sizing transparency columns.
        # entry_size_usdt = original USDT deployed when trade opened.
        # exit_size_usdt  = USDT actually closed (< entry for partial closes).
        # Both nullable — pre-Session-30 rows fall back to size_usdt in to_dict().
        ("paper_trades",  "entry_size_usdt", "REAL"),
        ("paper_trades",  "exit_size_usdt",  "REAL"),
        # Session 35: trade_feedback table columns (full table created by
        # create_all() on first run; only the individual column migrations
        # are needed for upgrading existing DBs that already have the table
        # from a partial deployment).
        ("trade_feedback", "setup_score",      "REAL DEFAULT 0.0"),
        ("trade_feedback", "risk_score",       "REAL DEFAULT 0.0"),
        ("trade_feedback", "execution_score",  "REAL DEFAULT 0.0"),
        ("trade_feedback", "decision_score",   "REAL DEFAULT 0.0"),
        ("trade_feedback", "overall_score",    "REAL DEFAULT 0.0"),
        ("trade_feedback", "classification",   "VARCHAR(10) DEFAULT 'NEUTRAL'"),
        ("trade_feedback", "hard_overrides",   "TEXT"),
        ("trade_feedback", "root_causes",      "TEXT"),
        ("trade_feedback", "recommendations",  "TEXT"),
        ("trade_feedback", "penalty_log",      "TEXT"),
        ("trade_feedback", "ai_explanation",   "TEXT"),
        ("trade_feedback", "pnl_usdt",         "REAL DEFAULT 0.0"),
        ("trade_feedback", "pnl_pct",          "REAL DEFAULT 0.0"),
        ("trade_feedback", "exit_reason",      "VARCHAR(30) DEFAULT ''"),
        ("trade_feedback", "duration_s",       "INTEGER DEFAULT 0"),
        # Session 36: Phase 2 decision forensics + scoring extras
        ("trade_feedback", "decision_outcome_matrix",    "VARCHAR(40)"),
        ("trade_feedback", "avoidable_loss_flag",        "BOOLEAN"),
        ("trade_feedback", "avoidable_win_flag",         "BOOLEAN"),
        ("trade_feedback", "was_loss_acceptable",        "BOOLEAN"),
        ("trade_feedback", "failure_domain_primary",     "VARCHAR(30)"),
        ("trade_feedback", "failure_domain_secondary",   "VARCHAR(30)"),
        ("trade_feedback", "preventability_score",       "REAL DEFAULT 0.0"),
        ("trade_feedback", "randomness_score",           "REAL DEFAULT 0.0"),
        ("trade_feedback", "model_conflict_score",       "REAL DEFAULT 0.0"),
        ("trade_feedback", "regime_confidence_at_entry", "REAL DEFAULT 0.0"),
        ("trade_feedback", "htf_confirmed_at_entry",     "BOOLEAN"),
        ("trade_feedback", "signal_conflict_score",      "REAL DEFAULT 0.0"),
        # Session 36: strategy_tuning_proposals table columns
        ("strategy_tuning_proposals", "proposal_id",              "VARCHAR(30)"),
        ("strategy_tuning_proposals", "root_cause_category",      "VARCHAR(50)"),
        ("strategy_tuning_proposals", "rec_id",                   "VARCHAR(80)"),
        ("strategy_tuning_proposals", "affected_subsystem",       "VARCHAR(100)"),
        ("strategy_tuning_proposals", "tuning_parameter",         "VARCHAR(100)"),
        ("strategy_tuning_proposals", "tuning_direction",         "VARCHAR(20)"),
        ("strategy_tuning_proposals", "proposed_change_description","TEXT"),
        ("strategy_tuning_proposals", "expected_benefit",         "TEXT"),
        ("strategy_tuning_proposals", "confidence",               "REAL DEFAULT 0.0"),
        ("strategy_tuning_proposals", "risk_level",               "VARCHAR(10) DEFAULT 'medium'"),
        ("strategy_tuning_proposals", "auto_tune_eligible",       "BOOLEAN DEFAULT 0"),
        ("strategy_tuning_proposals", "requires_manual_approval", "BOOLEAN DEFAULT 1"),
        ("strategy_tuning_proposals", "status",                   "VARCHAR(20) DEFAULT 'pending'"),
        # Session 36: applied_strategy_changes table columns
        ("applied_strategy_changes", "proposal_id",            "VARCHAR(30)"),
        ("applied_strategy_changes", "root_cause_category",    "VARCHAR(50)"),
        ("applied_strategy_changes", "tuning_parameter",       "VARCHAR(100)"),
        ("applied_strategy_changes", "tuning_direction",       "VARCHAR(20)"),
        ("applied_strategy_changes", "applied_value",          "TEXT"),
        ("applied_strategy_changes", "applied_by",             "VARCHAR(20) DEFAULT 'auto'"),
        ("applied_strategy_changes", "backtest_delta_pf_pct",  "REAL DEFAULT 0.0"),
        # Wave 2 Sub-wave 2.3: tuning_proposal_outcomes table.
        # create_all() creates the table on fresh DBs; these ALTER TABLE entries
        # upgrade existing DBs that were created before Wave 2 shipped.
        ("tuning_proposal_outcomes", "proposal_id",           "VARCHAR(30) NOT NULL DEFAULT ''"),
        ("tuning_proposal_outcomes", "status",                "VARCHAR(30) DEFAULT 'PENDING_EVALUATION'"),
        ("tuning_proposal_outcomes", "min_trades_threshold",  "INTEGER DEFAULT 30"),
        ("tuning_proposal_outcomes", "pre_trades",            "INTEGER DEFAULT 0"),
        ("tuning_proposal_outcomes", "pre_win_rate",          "REAL"),
        ("tuning_proposal_outcomes", "pre_profit_factor",     "REAL"),
        ("tuning_proposal_outcomes", "pre_avg_r",             "REAL"),
        ("tuning_proposal_outcomes", "post_trades",           "INTEGER DEFAULT 0"),
        ("tuning_proposal_outcomes", "post_win_rate",         "REAL"),
        ("tuning_proposal_outcomes", "post_profit_factor",    "REAL"),
        ("tuning_proposal_outcomes", "post_avg_r",            "REAL"),
        ("tuning_proposal_outcomes", "delta_win_rate",        "REAL"),
        ("tuning_proposal_outcomes", "delta_pf",              "REAL"),
        ("tuning_proposal_outcomes", "verdict",               "VARCHAR(20)"),
        ("tuning_proposal_outcomes", "notes",                 "TEXT"),
        ("tuning_proposal_outcomes", "applied_at",            "DATETIME"),
        ("tuning_proposal_outcomes", "measured_at",           "DATETIME"),
        ("tuning_proposal_outcomes", "created_at",            "DATETIME"),
    ]
    with engine.connect() as conn:
        for table, column, definition in migrations:
            try:
                conn.execute(text(f"SELECT {column} FROM {table} LIMIT 1"))
            except Exception:
                try:
                    conn.execute(text(
                        f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                    ))
                    conn.commit()
                    logger.info("Schema migration: added %s.%s", table, column)
                except Exception as exc:
                    logger.warning("Schema migration failed for %s.%s: %s",
                                   table, column, exc)


def _fix_ai_strategy_types():
    """
    One-time data fix: strategies saved as ai_generated=True but type='rule'
    (due to early bug where AI system prompt template used "type":"rule")
    should have their type updated to 'ai'.
    """
    try:
        with engine.connect() as conn:
            conn.execute(text(
                "UPDATE strategies SET type = 'ai' "
                "WHERE ai_generated = 1 AND type = 'rule'"
            ))
            conn.commit()
    except Exception as exc:
        logger.warning("_fix_ai_strategy_types: %s", exc)


def _seed_default_settings():
    """Insert default settings if they don't exist."""
    from core.database.models import Setting
    defaults = {
        "risk.max_position_pct": "2.0",
        "risk.max_portfolio_drawdown_pct": "15.0",
        "risk.max_strategy_drawdown_pct": "10.0",
        "risk.min_sharpe_live": "0.5",
        "risk.max_spread_pct": "0.3",
        "ai.openai_model": "gpt-4o",
        "ai.anthropic_model": "claude-opus-4-6",
        "app.theme": "dark",
        "data.default_timeframe": "1h",
    }
    with get_session() as session:
        for key, value in defaults.items():
            exists = session.get(Setting, key)
            if not exists:
                session.add(Setting(key=key, value=value))
        session.commit()


@contextmanager
def get_session() -> Session:
    """Context manager for database sessions with automatic cleanup."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Session:
    """Dependency-injection style session getter."""
    return SessionLocal()
