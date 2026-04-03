# ============================================================
# NEXUS TRADER Web — PostgreSQL Database Engine
#
# Replaces SQLite engine with PostgreSQL via SQLAlchemy 2.0.
# Provides async-compatible session management for FastAPI.
# ============================================================
from __future__ import annotations

import logging
from contextlib import asynccontextmanager, contextmanager
from typing import AsyncGenerator, Generator

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

logger = logging.getLogger(__name__)


# ── Declarative Base ────────────────────────────────────────
class Base(DeclarativeBase):
    """SQLAlchemy declarative base for all web models."""
    pass


# ── Sync Engine (for Alembic migrations and scripts) ────────
def _build_sync_url(url: str) -> str:
    """Convert async URL to sync if needed."""
    return url.replace("postgresql+asyncpg://", "postgresql://")


def get_sync_engine():
    settings = get_settings()
    return create_engine(
        _build_sync_url(settings.database_url),
        echo=settings.debug,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
    )


def get_sync_session_factory():
    engine = get_sync_engine()
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


@contextmanager
def get_sync_session() -> Generator[Session, None, None]:
    """Sync session for migrations, scripts, and CLI tools."""
    factory = get_sync_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ── Async Engine (for FastAPI request handling) ─────────────
def _build_async_url(url: str) -> str:
    """Ensure URL uses asyncpg driver."""
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


_async_engine = None
_async_session_factory = None


def get_async_engine():
    global _async_engine
    if _async_engine is None:
        settings = get_settings()
        _async_engine = create_async_engine(
            _build_async_url(settings.database_url),
            echo=settings.debug,
            pool_size=10,
            max_overflow=20,
            pool_pre_ping=True,
        )
    return _async_engine


def get_async_session_factory():
    global _async_session_factory
    if _async_session_factory is None:
        engine = get_async_engine()
        _async_session_factory = async_sessionmaker(
            bind=engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _async_session_factory


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: yields an async DB session per request."""
    factory = get_async_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ── Lifecycle ───────────────────────────────────────────────
async def init_async_engine():
    """Call on app startup to verify connection."""
    engine = get_async_engine()
    async with engine.begin() as conn:
        # Simple connectivity check
        await conn.execute(
            __import__("sqlalchemy").text("SELECT 1")
        )
    logger.info("PostgreSQL async engine connected")


async def dispose_async_engine():
    """Call on app shutdown to close pool."""
    global _async_engine, _async_session_factory
    if _async_engine is not None:
        await _async_engine.dispose()
        _async_engine = None
        _async_session_factory = None
    logger.info("PostgreSQL async engine disposed")
