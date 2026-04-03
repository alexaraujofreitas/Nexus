# ============================================================
# NEXUS TRADER Web — Auth Router
#
# POST /auth/login    — email + password → access + refresh tokens
# POST /auth/refresh  — refresh token → new access token
# POST /auth/logout   — revoke refresh token
# POST /auth/setup    — initial user creation (first-run only)
# ============================================================
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.jwt import (
    create_access_token,
    create_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)
from app.config import get_settings
from app.database import get_db
from app.middleware.rate_limit import limiter, get_auth_limit
from app.models.auth import RefreshToken, User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])

# ── Account Lockout Constants ──────────────────────────────
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_DURATION_MINUTES = 15


# ── Password Complexity ───────────────────────────────────
def validate_password_complexity(password: str) -> list[str]:
    """
    Validate password meets complexity requirements.
    Returns list of violation messages (empty = valid).
    """
    errors = []
    if len(password) < 12:
        errors.append("Password must be at least 12 characters")
    if not any(c.isupper() for c in password):
        errors.append("Password must contain at least one uppercase letter")
    if not any(c.islower() for c in password):
        errors.append("Password must contain at least one lowercase letter")
    if not any(c.isdigit() for c in password):
        errors.append("Password must contain at least one digit")
    if not any(c in "!@#$%^&*()_+-=[]{}|;:',.<>?/`~" for c in password):
        errors.append("Password must contain at least one special character")
    return errors


# ── Request / Response Schemas ──────────────────────────────
class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class AccessTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class SetupRequest(BaseModel):
    email: EmailStr
    password: str
    display_name: str | None = None

    @field_validator("password")
    @classmethod
    def check_password_complexity(cls, v: str) -> str:
        errors = validate_password_complexity(v)
        if errors:
            raise ValueError("; ".join(errors))
        return v


# ── Endpoints ───────────────────────────────────────────────

@router.post("/setup", response_model=TokenResponse, status_code=201)
@limiter.limit(get_auth_limit)
async def setup_first_user(
    request: Request,
    body: SetupRequest,
    db: AsyncSession = Depends(get_db),
):
    """Create the initial admin user. Only works when no users exist (idempotent)."""
    result = await db.execute(select(User).limit(1))
    if result.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Setup already completed — user exists",
        )

    # Password complexity is enforced by SetupRequest's Pydantic field_validator
    user = User(
        email=body.email,
        hashed_password=hash_password(body.password),
        display_name=body.display_name,
        is_admin=True,
    )
    db.add(user)
    await db.flush()

    access_token = create_access_token({"sub": str(user.id), "email": user.email})
    raw_refresh, token_hash = create_refresh_token()

    settings = get_settings()
    db.add(RefreshToken(
        user_id=user.id,
        token_hash=token_hash,
        expires_at=datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_expire_days),
    ))

    return TokenResponse(access_token=access_token, refresh_token=raw_refresh)


@router.post("/login", response_model=TokenResponse)
@limiter.limit(get_auth_limit)
async def login(
    request: Request,
    body: LoginRequest,
    db: AsyncSession = Depends(get_db),
):
    """Authenticate with email + password. Account locks after 5 failures."""
    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    # Check account lockout
    now = datetime.now(timezone.utc)
    if user.locked_until and user.locked_until > now:
        remaining = int((user.locked_until - now).total_seconds() / 60) + 1
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail=f"Account locked due to too many failed attempts. Try again in {remaining} minute(s).",
        )

    if not verify_password(body.password, user.hashed_password):
        # Increment failure counter
        user.failed_login_attempts = (user.failed_login_attempts or 0) + 1
        if user.failed_login_attempts >= MAX_LOGIN_ATTEMPTS:
            user.locked_until = now + timedelta(minutes=LOCKOUT_DURATION_MINUTES)
            logger.warning(
                "Account locked for user %s after %d failed attempts",
                user.email, user.failed_login_attempts,
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account disabled",
        )

    # Reset lockout on successful login
    user.failed_login_attempts = 0
    user.locked_until = None
    user.last_login = now

    access_token = create_access_token({"sub": str(user.id), "email": user.email})
    raw_refresh, token_hash = create_refresh_token()

    settings = get_settings()
    db.add(RefreshToken(
        user_id=user.id,
        token_hash=token_hash,
        expires_at=datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_expire_days),
    ))

    return TokenResponse(access_token=access_token, refresh_token=raw_refresh)


@router.post("/refresh", response_model=AccessTokenResponse)
async def refresh(
    body: RefreshRequest,
    db: AsyncSession = Depends(get_db),
):
    """Exchange a refresh token for a new access token."""
    token_hash = hash_refresh_token(body.refresh_token)
    result = await db.execute(
        select(RefreshToken).where(
            RefreshToken.token_hash == token_hash,
            RefreshToken.revoked == False,  # noqa: E712
        )
    )
    stored = result.scalar_one_or_none()

    if stored is None:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    if stored.expires_at < datetime.now(timezone.utc):
        stored.revoked = True
        raise HTTPException(status_code=401, detail="Refresh token expired")

    user_result = await db.execute(select(User).where(User.id == stored.user_id))
    user = user_result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=403, detail="Account disabled or deleted")

    access_token = create_access_token({"sub": str(user.id), "email": user.email})
    return AccessTokenResponse(access_token=access_token)


@router.post("/logout", status_code=204)
async def logout(
    body: RefreshRequest,
    db: AsyncSession = Depends(get_db),
):
    """Revoke a refresh token."""
    token_hash = hash_refresh_token(body.refresh_token)
    result = await db.execute(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    )
    stored = result.scalar_one_or_none()
    if stored is not None:
        stored.revoked = True
        stored.revoked_at = datetime.now(timezone.utc)


@router.get("/me")
async def me(current_user: dict = Depends(get_current_user)):
    """Return current user info from JWT."""
    return {"user_id": current_user["sub"], "email": current_user["email"]}
