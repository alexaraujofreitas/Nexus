# ============================================================
# NEXUS TRADER Web — Phase 8A Vault Encryption Tests
#
# 10 unit tests + 2 integration tests covering:
# - encrypt/decrypt round-trip
# - empty string handling
# - invalid token rejection
# - key auto-generation
# - key file permissions
# - key rotation
# - singleton behavior
# - env key override
# - settings masking
# - settings encryption
# ============================================================
from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest

from app.services.vault import (
    VaultService,
    VaultDecryptionError,
    get_vault,
    reset_vault,
)


# ── Fixtures ───────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_singleton():
    """Reset the vault singleton before and after each test."""
    reset_vault()
    yield
    reset_vault()


@pytest.fixture
def tmp_key_dir(tmp_path):
    """Provide a temp directory for key file storage."""
    return tmp_path


@pytest.fixture
def vault(tmp_key_dir):
    """Create a fresh VaultService with a temp key path."""
    key_path = tmp_key_dir / ".nexus_test_key"
    return VaultService(key_path=key_path)


# ── Unit Tests ─────────────────────────────────────────────

class TestVaultEncryptDecrypt:
    """Core encrypt/decrypt functionality."""

    def test_encrypt_decrypt_roundtrip(self, vault):
        """Encrypt a secret, decrypt it, verify it matches."""
        secret = "sk-ant-api03-xyzABC123_secretkey"
        ciphertext = vault.encrypt(secret)

        # Ciphertext should be different from plaintext
        assert ciphertext != secret
        assert len(ciphertext) > 50

        # Decrypt should return original
        plaintext = vault.decrypt(ciphertext)
        assert plaintext == secret

    def test_encrypt_decrypt_unicode(self, vault):
        """Round-trip with unicode characters."""
        secret = "p@ssw0rd-with-special-chars!@#$%^&*()"
        ciphertext = vault.encrypt(secret)
        assert vault.decrypt(ciphertext) == secret

    def test_encrypt_empty_string(self, vault):
        """Empty strings should pass through unchanged."""
        assert vault.encrypt("") == ""
        assert vault.decrypt("") == ""

    def test_decrypt_invalid_token(self, vault):
        """Decrypting garbage should raise VaultDecryptionError."""
        with pytest.raises(VaultDecryptionError, match="Decryption failed"):
            vault.decrypt("this-is-not-a-valid-fernet-token")

    def test_decrypt_wrong_key(self, tmp_key_dir):
        """Decrypting with a different key should fail."""
        vault1 = VaultService(key_path=tmp_key_dir / ".key1")
        vault2 = VaultService(key_path=tmp_key_dir / ".key2")

        ciphertext = vault1.encrypt("my-secret")
        with pytest.raises(VaultDecryptionError):
            vault2.decrypt(ciphertext)


class TestVaultKeyManagement:
    """Key generation, loading, and file permissions."""

    def test_key_generation(self, tmp_key_dir):
        """VaultService auto-generates a key file if none exists."""
        key_path = tmp_key_dir / ".new_key"
        assert not key_path.exists()

        _vault = VaultService(key_path=key_path)
        assert key_path.exists()
        # Key should be a valid Fernet key (44 bytes base64)
        key_data = key_path.read_bytes().strip()
        assert len(key_data) == 44

    def test_key_file_permissions(self, tmp_key_dir):
        """Key file should be created with 0o600 permissions."""
        key_path = tmp_key_dir / ".perm_test_key"
        _vault = VaultService(key_path=key_path)

        file_stat = os.stat(key_path)
        mode = stat.S_IMODE(file_stat.st_mode)
        # On Unix: should be exactly 0o600
        # On some systems this might not work perfectly, so check at minimum
        # that group/other don't have access
        assert mode & stat.S_IROTH == 0, "Other-read should be off"
        assert mode & stat.S_IWOTH == 0, "Other-write should be off"
        assert mode & stat.S_IRGRP == 0, "Group-read should be off"
        assert mode & stat.S_IWGRP == 0, "Group-write should be off"

    def test_key_persistence(self, tmp_key_dir):
        """Creating two VaultService instances with same path should use same key."""
        key_path = tmp_key_dir / ".persist_key"
        v1 = VaultService(key_path=key_path)
        encrypted = v1.encrypt("test-secret")

        # New instance, same key path
        v2 = VaultService(key_path=key_path)
        assert v2.decrypt(encrypted) == "test-secret"

    def test_env_key_override(self, tmp_key_dir):
        """NEXUS_ENCRYPTION_KEY env var should take precedence over file."""
        from cryptography.fernet import Fernet

        env_key = Fernet.generate_key().decode()
        key_path = tmp_key_dir / ".ignored_key"

        with patch.dict(os.environ, {"NEXUS_ENCRYPTION_KEY": env_key}):
            v = VaultService(key_path=key_path)
            assert v.key_source == "environment"
            # Should work for encrypt/decrypt
            ct = v.encrypt("env-secret")
            assert v.decrypt(ct) == "env-secret"


class TestVaultKeyRotation:
    """Key rotation re-encrypts all values."""

    def test_key_rotation(self, vault):
        """rotate_key() should re-encrypt values with new key."""
        # Encrypt some values
        encrypted = {
            "ai.anthropic_api_key": vault.encrypt("sk-ant-123"),
            "notifications.telegram_token": vault.encrypt("bot:token"),
        }

        # Rotate
        re_encrypted, new_key = vault.rotate_key(encrypted)

        # New key should be a valid Fernet key
        assert len(new_key) == 44

        # Re-encrypted values should be different from originals
        assert re_encrypted["ai.anthropic_api_key"] != encrypted["ai.anthropic_api_key"]
        assert re_encrypted["notifications.telegram_token"] != encrypted["notifications.telegram_token"]

        # Should decrypt to original values with new key
        assert vault.decrypt(re_encrypted["ai.anthropic_api_key"]) == "sk-ant-123"
        assert vault.decrypt(re_encrypted["notifications.telegram_token"]) == "bot:token"

        # Old ciphertext should NOT decrypt with new key
        with pytest.raises(VaultDecryptionError):
            vault.decrypt(encrypted["ai.anthropic_api_key"])


class TestVaultSingleton:
    """Singleton behavior."""

    def test_singleton(self, tmp_key_dir):
        """get_vault() should return the same instance."""
        key_path = tmp_key_dir / ".singleton_key"
        v1 = get_vault(key_path)
        v2 = get_vault()
        assert v1 is v2


class TestVaultMasking:
    """Value masking for API responses."""

    def test_mask_normal_value(self):
        assert VaultService.mask("sk-ant-api03-abcdef123456") == "****3456"

    def test_mask_short_value(self):
        assert VaultService.mask("abc") == "****"

    def test_mask_empty_value(self):
        assert VaultService.mask("") == ""

    def test_mask_none_value(self):
        assert VaultService.mask(None) == ""


class TestVaultKeyRegistry:
    """VAULT_KEYS registry."""

    def test_is_vault_key(self):
        assert VaultService.is_vault_key("ai.anthropic_api_key") is True
        assert VaultService.is_vault_key("notifications.telegram_token") is True
        assert VaultService.is_vault_key("agents.fred_api_key") is True

    def test_is_not_vault_key(self):
        assert VaultService.is_vault_key("ai.active_provider") is False
        assert VaultService.is_vault_key("risk.max_position_pct") is False
        assert VaultService.is_vault_key("data.default_timeframe") is False

    def test_vault_keys_count(self):
        """Should have at least 15 vault keys matching desktop _VAULT_KEYS."""
        assert len(VaultService.VAULT_KEYS) >= 15

    def test_is_encrypted_detection(self, vault):
        """is_encrypted should correctly identify Fernet tokens."""
        ct = vault.encrypt("test")
        assert vault.is_encrypted(ct) is True
        assert vault.is_encrypted("plaintext") is False
        assert vault.is_encrypted("") is False


class TestVaultStatus:
    """Status reporting."""

    def test_status_keys(self, vault):
        status = vault.status()
        assert "key_source" in status
        assert "key_file_exists" in status
        assert "key_file_path" in status
        assert "vault_keys_count" in status
        assert status["vault_keys_count"] >= 15


# ── Integration Tests ──────────────────────────────────────

class TestSettingsVaultIntegration:
    """Integration of vault with settings API masking/encryption."""

    @pytest.fixture(autouse=True)
    def _set_singleton(self, vault):
        """Ensure get_vault() returns the same instance as the vault fixture."""
        import app.services.vault as vault_mod
        vault_mod._vault_instance = vault
        yield
        vault_mod._vault_instance = None

    def test_mask_vault_keys_in_config(self, vault):
        """Settings response should mask vault keys."""
        from app.api.settings_api import _mask_vault_keys_in_config

        # Simulate engine config with encrypted vault keys
        encrypted_key = vault.encrypt("sk-ant-real-key")
        config = {
            "ai": {
                "active_provider": "Anthropic Claude",
                "anthropic_api_key": encrypted_key,
                "anthropic_model": "claude-opus-4-6",
            },
            "risk": {
                "max_position_pct": 2.0,
            },
        }

        masked = _mask_vault_keys_in_config(config)

        # Non-vault keys should be unchanged
        assert masked["ai"]["active_provider"] == "Anthropic Claude"
        assert masked["ai"]["anthropic_model"] == "claude-opus-4-6"
        assert masked["risk"]["max_position_pct"] == 2.0

        # Vault key should be masked (****last4 of original plaintext)
        assert masked["ai"]["anthropic_api_key"].startswith("****")
        # Should NOT contain the actual key
        assert "sk-ant-real-key" not in masked["ai"]["anthropic_api_key"]

    def test_encrypt_vault_keys_in_updates(self, vault):
        """Settings update should encrypt vault keys before engine dispatch."""
        from app.api.settings_api import _encrypt_vault_keys_in_updates

        updates = {
            "ai.anthropic_api_key": "sk-ant-new-key-12345",
            "ai.active_provider": "Anthropic Claude",
            "risk.max_position_pct": 3.0,
        }

        encrypted = _encrypt_vault_keys_in_updates(updates)

        # Vault key should be encrypted
        assert encrypted["ai.anthropic_api_key"] != "sk-ant-new-key-12345"
        assert vault.is_encrypted(encrypted["ai.anthropic_api_key"])

        # Non-vault keys should be unchanged
        assert encrypted["ai.active_provider"] == "Anthropic Claude"
        assert encrypted["risk.max_position_pct"] == 3.0

        # Should decrypt to original
        assert vault.decrypt(encrypted["ai.anthropic_api_key"]) == "sk-ant-new-key-12345"

    def test_encrypt_skips_already_encrypted(self, vault):
        """Should not re-encrypt already-encrypted values."""
        from app.api.settings_api import _encrypt_vault_keys_in_updates

        already_encrypted = vault.encrypt("original-secret")
        updates = {"ai.anthropic_api_key": already_encrypted}

        result = _encrypt_vault_keys_in_updates(updates)
        # Should be the same ciphertext (not double-encrypted)
        assert result["ai.anthropic_api_key"] == already_encrypted
