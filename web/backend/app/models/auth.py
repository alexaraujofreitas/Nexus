# ============================================================
# NEXUS TRADER Web — Auth Models (Web-only)
#
# These tables do NOT exist in the desktop SQLite database.
# They are created fresh in PostgreSQL for web authentication.
# ============================================================
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String, Text, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class User(Base):
    """
    Web application user. Single-user system for now (operator only),
    but designed to support multi-user if needed later.
    """
    __tablename__ = "web_users"

    id:              Mapped[int]            = mapped_column(Integer, primary_key=True, autoincrement=True)
    email:           Mapped[str]            = mapped_column(String(255), nullable=False, unique=True)
    hashed_password: Mapped[str]            = mapped_column(Text, nullable=False)
    is_active:       Mapped[bool]           = mapped_column(Boolean, default=True)
    is_admin:        Mapped[bool]           = mapped_column(Boolean, default=True)
    display_name:    Mapped[Optional[str]]  = mapped_column(String(100), nullable=True)
    created_at:      Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    last_login:      Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Account lockout (Phase 6B)
    failed_login_attempts: Mapped[int]              = mapped_column(Integer, default=0, server_default="0")
    locked_until:          Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self):
        return f"<User {self.email} active={self.is_active}>"


class RefreshToken(Base):
    """
    Stores refresh tokens in PostgreSQL for revocation support.
    Access tokens are stateless JWT — only refresh tokens are tracked.
    """
    __tablename__ = "web_refresh_tokens"

    id:          Mapped[int]      = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id:     Mapped[int]      = mapped_column(Integer, nullable=False, index=True)
    token_hash:  Mapped[str]      = mapped_column(String(255), nullable=False, unique=True)
    expires_at:  Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at:  Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    revoked:     Mapped[bool]     = mapped_column(Boolean, default=False)
    revoked_at:  Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self):
        return f"<RefreshToken user={self.user_id} revoked={self.revoked}>"
