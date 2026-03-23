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
