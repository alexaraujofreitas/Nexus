# ============================================================
# Phase 6B Tests — Configuration Validation
#
# Validates:
#   - Empty JWT secret detected in production mode
#   - Short JWT secret rejected
#   - Empty database URL rejected
#   - Empty Redis URL rejected
#   - Missing encryption key warning in production
#   - Debug mode allows empty JWT
# ============================================================
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


class TestValidateSettings:
    """Test validate_settings() against various config states."""

    def _make_settings(self, **overrides):
        from app.config import Settings
        defaults = {
            "jwt_secret": "a-very-secure-secret-key-32chars!!",
            "database_url": "postgresql://nexus:nexus@localhost:5432/nexustrader",
            "redis_url": "redis://localhost:6379/0",
            "encryption_key": "some-fernet-key",
            "debug": False,
        }
        defaults.update(overrides)
        return Settings(**defaults)

    def test_valid_production_config(self):
        from app.config import validate_settings
        s = self._make_settings()
        errors = validate_settings(s)
        assert errors == []

    def test_empty_jwt_in_production(self):
        from app.config import validate_settings
        s = self._make_settings(jwt_secret="", debug=False)
        errors = validate_settings(s)
        assert any("empty" in e.lower() or "jwt" in e.lower() for e in errors)

    def test_empty_jwt_allowed_in_debug(self):
        from app.config import validate_settings
        s = self._make_settings(jwt_secret="", debug=True)
        errors = validate_settings(s)
        # In debug mode, empty JWT is not a fatal error (but short-length still is)
        fatal_empty = [e for e in errors if "empty" in e.lower() and "jwt" in e.lower()]
        assert len(fatal_empty) == 0

    def test_short_jwt_secret_rejected(self):
        from app.config import validate_settings
        s = self._make_settings(jwt_secret="short")
        errors = validate_settings(s)
        assert any("32 characters" in e for e in errors)

    def test_empty_database_url(self):
        from app.config import validate_settings
        s = self._make_settings(database_url="")
        errors = validate_settings(s)
        assert any("database" in e.lower() for e in errors)

    def test_empty_redis_url(self):
        from app.config import validate_settings
        s = self._make_settings(redis_url="")
        errors = validate_settings(s)
        assert any("redis" in e.lower() for e in errors)

    def test_missing_encryption_key_warning_production(self):
        from app.config import validate_settings
        s = self._make_settings(encryption_key="", debug=False)
        errors = validate_settings(s)
        assert any("encryption" in e.lower() for e in errors)

    def test_missing_encryption_key_ok_in_debug(self):
        from app.config import validate_settings
        s = self._make_settings(encryption_key="", debug=True)
        errors = validate_settings(s)
        enc_errors = [e for e in errors if "encryption" in e.lower()]
        assert len(enc_errors) == 0
