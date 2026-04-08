# ============================================================
# NEXUS TRADER Web — Application Configuration
#
# All config from environment variables with sensible defaults.
# ============================================================
from __future__ import annotations

import os
from functools import lru_cache
from pydantic import BaseModel


class Settings(BaseModel):
    """Application settings loaded from environment variables.

    All values are read from ``os.environ`` at *instantiation* time
    (via factory defaults), so they respect ``unittest.mock.patch.dict``.
    """

    # ── Service identity ────────────────────────────────────
    service_name: str = ""
    debug: bool = False

    # ── Database ────────────────────────────────────────────
    database_url: str = ""

    # ── Redis ───────────────────────────────────────────────
    redis_url: str = ""

    # ── JWT Auth ────────────────────────────────────────────
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7

    # ── CORS ────────────────────────────────────────────────
    cors_origins: list[str] = []

    # ── NexusTrader core config path ────────────────────────
    nexus_config_path: str = ""
    nexus_root_dir: str = ""

    # ── Rate Limiting ───────────────────────────────────────
    rate_limit_global: int = 100
    rate_limit_auth: int = 5
    rate_limit_commands: int = 10

    # ── Cloudflare Zero Trust Access ──────────────────────────
    cf_enabled: bool = False
    cf_team_domain: str = ""
    cf_audience: str = ""

    # ── Encryption ──────────────────────────────────────────
    encryption_key: str = ""

    def __init__(self, **kwargs):
        """Read all config from environment at construction time."""
        defaults = {
            "service_name": os.getenv("NEXUS_SERVICE_NAME", "api"),
            "debug": os.getenv("NEXUS_DEBUG", "false").lower() == "true",
            "database_url": os.getenv(
                "NEXUS_DATABASE_URL",
                "postgresql://nexus:nexus@localhost:5432/nexustrader",
            ),
            "redis_url": os.getenv("NEXUS_REDIS_URL", "redis://localhost:6379/0"),
            "jwt_secret": os.getenv("NEXUS_JWT_SECRET", ""),
            "cors_origins": os.getenv(
                "NEXUS_CORS_ORIGINS",
                "http://localhost:3000,http://localhost:5173",
            ).split(","),
            "nexus_config_path": os.getenv("NEXUS_CONFIG_PATH", "/app/config.yaml"),
            "nexus_root_dir": os.getenv("NEXUS_ROOT_DIR", "/app"),
            "rate_limit_global": int(os.getenv("NEXUS_RATE_LIMIT_GLOBAL", "100")),
            "rate_limit_auth": int(os.getenv("NEXUS_RATE_LIMIT_AUTH", "5")),
            "rate_limit_commands": int(os.getenv("NEXUS_RATE_LIMIT_COMMANDS", "10")),
            "cf_enabled": os.getenv("NEXUS_CF_ENABLED", "false").lower() == "true",
            "cf_team_domain": os.getenv("NEXUS_CF_TEAM_DOMAIN", ""),
            "cf_audience": os.getenv("NEXUS_CF_AUDIENCE", ""),
            "encryption_key": os.getenv("NEXUS_ENCRYPTION_KEY", ""),
        }
        # Explicit kwargs override env-based defaults
        defaults.update(kwargs)
        super().__init__(**defaults)


# ── Singleton with cache-clear support ─────────────────────
_settings: Settings | None = None


class ConfigurationError(RuntimeError):
    """Raised when critical configuration is missing or insecure."""


def validate_settings(s: Settings) -> list[str]:
    """
    Validate critical configuration values. Returns list of error messages.
    In production (debug=False), empty or short JWT secret is fatal.
    """
    errors: list[str] = []

    # JWT secret must not be empty in production
    if not s.debug and not s.jwt_secret:
        errors.append(
            "NEXUS_JWT_SECRET is empty. "
            "Set a secure 32+ character secret via environment variable."
        )

    # JWT secret minimum length (production only — dev mode uses any non-empty secret)
    if not s.debug and s.jwt_secret and len(s.jwt_secret) < 32:
        errors.append(
            f"NEXUS_JWT_SECRET is only {len(s.jwt_secret)} characters. "
            "Minimum 32 characters required."
        )

    # Database URL must be set and not empty
    if not s.database_url:
        errors.append("NEXUS_DATABASE_URL is empty. A PostgreSQL URL is required.")

    # Redis URL must be set
    if not s.redis_url:
        errors.append("NEXUS_REDIS_URL is empty. A Redis URL is required.")

    # Encryption key warning (non-fatal but logged)
    if not s.encryption_key and not s.debug:
        errors.append(
            "NEXUS_ENCRYPTION_KEY is empty. Set a Fernet key for data encryption."
        )

    return errors


def get_settings() -> Settings:
    """Return cached Settings instance.  Call ``clear_settings()`` to reset."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def clear_settings() -> None:
    """Reset the cached Settings so the next ``get_settings()`` re-reads env."""
    global _settings
    _settings = None
