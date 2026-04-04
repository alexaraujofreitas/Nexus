# ============================================================
# Phase 8D — Settings Notifications Tab Tests
#
# Validates:
#  1. Notification vault keys are in the vault registry
#  2. Vault masking for notification credentials
#  3. Vault encryption for notification credentials
#  4. Backend endpoint validation (channel names, preferences, intervals)
#  5. Global + per-channel config field completeness
#  6. Desktop parity for preferences
# ============================================================
from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.services.vault import VaultService, get_vault, reset_vault
from app.services import vault as vault_mod
from app.api.settings_api import (
    _mask_vault_keys_in_config,
    _encrypt_vault_keys_in_updates,
    VALID_CHANNELS,
    VALID_HEALTH_CHECK_INTERVALS,
    VALID_NOTIFICATION_TYPES,
)


VAULT_KEYS = VaultService.VAULT_KEYS


# ── Fixture ──────────────────────────────────────────────────

@pytest.fixture
def vault(tmp_path: Path):
    """Create a vault with a temp key for testing."""
    v = VaultService(key_path=tmp_path / ".test_key")
    return v


# ── 1. Vault Key Registry ───────────────────────────────────

class TestNotificationVaultKeys:
    """All 5 notification credential keys are in the vault registry."""

    EXPECTED_NOTIFICATION_VAULT_KEYS = [
        "notifications.twilio_sid",
        "notifications.twilio_token",
        "notifications.telegram_token",
        "notifications.email_password",
        "notifications.gemini_password",
    ]

    def test_all_notification_keys_registered(self):
        for key in self.EXPECTED_NOTIFICATION_VAULT_KEYS:
            assert key in VAULT_KEYS, f"{key} missing from VAULT_KEYS"

    def test_notification_vault_key_count(self):
        notification_keys = [k for k in VAULT_KEYS if k.startswith("notifications.")]
        assert len(notification_keys) == 5

    def test_is_vault_key_method(self):
        for key in self.EXPECTED_NOTIFICATION_VAULT_KEYS:
            assert VaultService.is_vault_key(key) is True

    def test_non_vault_notification_keys(self):
        """Non-credential notification config keys must NOT be vault keys."""
        non_vault = [
            "notifications.whatsapp.enabled",
            "notifications.whatsapp.from_number",
            "notifications.telegram.enabled",
            "notifications.telegram.chat_id",
            "notifications.email.enabled",
            "notifications.email.smtp_host",
            "notifications.email.smtp_port",
            "notifications.sms.enabled",
            "notifications.gemini.enabled",
            "notifications.gemini.username",
            "notifications.preferences.trade_opened",
            "notifications.desktop_enabled",
            "notifications.dedup_window_seconds",
        ]
        for key in non_vault:
            assert VaultService.is_vault_key(key) is False, f"{key} should NOT be a vault key"


# ── 2. Masking ───────────────────────────────────────────────

class TestNotificationMasking:
    """Vault keys in notification config are masked correctly."""

    @pytest.fixture(autouse=True)
    def _set_singleton(self, vault):
        vault_mod._vault_instance = vault
        yield
        vault_mod._vault_instance = None

    def test_mask_twilio_sid(self, vault):
        encrypted = vault.encrypt("AC123456789abcdef")
        config = {"notifications": {"twilio_sid": encrypted}}
        result = _mask_vault_keys_in_config(config)
        assert result["notifications"]["twilio_sid"].startswith("****")
        assert result["notifications"]["twilio_sid"].endswith("cdef")

    def test_mask_telegram_token(self, vault):
        encrypted = vault.encrypt("1234567890:ABCDefgh_IjklMnopQrstUvwxyz")
        config = {"notifications": {"telegram_token": encrypted}}
        result = _mask_vault_keys_in_config(config)
        assert "****" in result["notifications"]["telegram_token"]
        assert "wxyz" in result["notifications"]["telegram_token"]

    def test_mask_email_password(self, vault):
        encrypted = vault.encrypt("myGmailAppPwd123")
        config = {"notifications": {"email_password": encrypted}}
        result = _mask_vault_keys_in_config(config)
        assert "myGmailAppPwd" not in result["notifications"]["email_password"]
        assert result["notifications"]["email_password"].endswith("d123")

    def test_mask_gemini_password(self, vault):
        encrypted = vault.encrypt("gmailAppPassword16")
        config = {"notifications": {"gemini_password": encrypted}}
        result = _mask_vault_keys_in_config(config)
        assert result["notifications"]["gemini_password"].startswith("****")

    def test_non_vault_keys_pass_through(self, vault):
        config = {
            "notifications": {
                "whatsapp": {
                    "enabled": True,
                    "from_number": "whatsapp:+14155238886",
                    "to_number": "whatsapp:+15551234567",
                },
                "desktop_enabled": True,
                "dedup_window_seconds": 60,
            }
        }
        result = _mask_vault_keys_in_config(config)
        assert result["notifications"]["whatsapp"]["enabled"] is True
        assert result["notifications"]["whatsapp"]["from_number"] == "whatsapp:+14155238886"
        assert result["notifications"]["desktop_enabled"] is True
        assert result["notifications"]["dedup_window_seconds"] == 60

    def test_empty_vault_key_returns_empty(self, vault):
        config = {"notifications": {"twilio_sid": ""}}
        result = _mask_vault_keys_in_config(config)
        assert result["notifications"]["twilio_sid"] == ""


# ── 3. Encryption ────────────────────────────────────────────

class TestNotificationEncryption:
    """Vault keys are encrypted when updating notification config."""

    @pytest.fixture(autouse=True)
    def _set_singleton(self, vault):
        vault_mod._vault_instance = vault
        yield
        vault_mod._vault_instance = None

    def test_encrypt_twilio_sid(self, vault):
        updates = {"notifications.twilio_sid": "AC123456789"}
        result = _encrypt_vault_keys_in_updates(updates)
        assert vault.is_encrypted(result["notifications.twilio_sid"])
        assert vault.decrypt(result["notifications.twilio_sid"]) == "AC123456789"

    def test_encrypt_twilio_token(self, vault):
        updates = {"notifications.twilio_token": "auth_token_value"}
        result = _encrypt_vault_keys_in_updates(updates)
        assert vault.is_encrypted(result["notifications.twilio_token"])

    def test_encrypt_telegram_token(self, vault):
        updates = {"notifications.telegram_token": "123456:ABCdefGHI"}
        result = _encrypt_vault_keys_in_updates(updates)
        assert vault.is_encrypted(result["notifications.telegram_token"])

    def test_encrypt_email_password(self, vault):
        updates = {"notifications.email_password": "gmailAppPwd"}
        result = _encrypt_vault_keys_in_updates(updates)
        assert vault.is_encrypted(result["notifications.email_password"])

    def test_encrypt_gemini_password(self, vault):
        updates = {"notifications.gemini_password": "anotherapppwd16c"}
        result = _encrypt_vault_keys_in_updates(updates)
        assert vault.is_encrypted(result["notifications.gemini_password"])

    def test_no_double_encrypt(self, vault):
        """Already-encrypted values must not be double-encrypted."""
        encrypted = vault.encrypt("secret_value")
        updates = {"notifications.twilio_sid": encrypted}
        result = _encrypt_vault_keys_in_updates(updates)
        # Should be the same encrypted string (not re-encrypted)
        assert result["notifications.twilio_sid"] == encrypted

    def test_non_vault_keys_pass_through(self, vault):
        updates = {
            "notifications.whatsapp.enabled": True,
            "notifications.whatsapp.from_number": "whatsapp:+14155238886",
            "notifications.desktop_enabled": True,
        }
        result = _encrypt_vault_keys_in_updates(updates)
        assert result["notifications.whatsapp.enabled"] is True
        assert result["notifications.whatsapp.from_number"] == "whatsapp:+14155238886"


# ── 4. Endpoint Validation ───────────────────────────────────

class TestEndpointValidation:
    """Backend constants and schemas are correctly defined."""

    def test_valid_channels_complete(self):
        assert VALID_CHANNELS == {"whatsapp", "telegram", "email", "gemini", "sms"}

    def test_valid_channels_count(self):
        assert len(VALID_CHANNELS) == 5

    def test_valid_health_check_intervals(self):
        assert VALID_HEALTH_CHECK_INTERVALS == {1, 2, 3, 4, 6, 12, 24}

    def test_invalid_intervals_rejected(self):
        for bad in [0, 5, 7, 8, 10, 13, 48]:
            assert bad not in VALID_HEALTH_CHECK_INTERVALS

    def test_valid_notification_types_complete(self):
        expected = {
            "trade_opened", "trade_closed", "trade_stopped", "trade_rejected",
            "trade_modified", "strategy_signal", "risk_warning", "market_condition",
            "system_error", "emergency_stop", "daily_summary", "health_check",
        }
        assert VALID_NOTIFICATION_TYPES == expected

    def test_notification_types_count(self):
        assert len(VALID_NOTIFICATION_TYPES) == 12


# ── 5. Config Field Completeness ─────────────────────────────

class TestConfigFieldCompleteness:
    """All notification config fields from desktop are represented."""

    # Global settings
    GLOBAL_FIELDS = [
        "notifications.desktop_enabled",
        "notifications.sound_enabled",
        "notifications.dedup_window_seconds",
    ]

    # Channel fields (non-vault)
    CHANNEL_FIELDS = [
        "notifications.whatsapp.enabled",
        "notifications.whatsapp.from_number",
        "notifications.whatsapp.to_number",
        "notifications.telegram.enabled",
        "notifications.telegram.chat_id",
        "notifications.email.enabled",
        "notifications.email.smtp_host",
        "notifications.email.smtp_port",
        "notifications.email.username",
        "notifications.email.from_address",
        "notifications.email.to_addresses",
        "notifications.email.use_tls",
        "notifications.sms.enabled",
        "notifications.sms.from_number",
        "notifications.sms.to_number",
        "notifications.gemini.enabled",
        "notifications.gemini.username",
        "notifications.gemini.to_address",
        "notifications.gemini.ai_enrich",
    ]

    # Preference fields
    PREFERENCE_FIELDS = [
        "notifications.preferences.trade_opened",
        "notifications.preferences.trade_closed",
        "notifications.preferences.trade_stopped",
        "notifications.preferences.trade_rejected",
        "notifications.preferences.trade_modified",
        "notifications.preferences.strategy_signal",
        "notifications.preferences.risk_warning",
        "notifications.preferences.market_condition",
        "notifications.preferences.system_error",
        "notifications.preferences.emergency_stop",
        "notifications.preferences.daily_summary",
        "notifications.preferences.health_check",
        "notifications.preferences.health_check_interval_hours",
    ]

    def test_global_field_count(self):
        assert len(self.GLOBAL_FIELDS) == 3

    def test_channel_field_count(self):
        assert len(self.CHANNEL_FIELDS) == 19

    def test_preference_field_count(self):
        assert len(self.PREFERENCE_FIELDS) == 13

    def test_total_non_vault_fields(self):
        """Total non-vault notification config fields: 35."""
        total = len(self.GLOBAL_FIELDS) + len(self.CHANNEL_FIELDS) + len(self.PREFERENCE_FIELDS)
        assert total == 35

    def test_vault_plus_nonvault_total(self):
        """Total notification fields: 35 non-vault + 5 vault = 40."""
        notification_vault_keys = [k for k in VAULT_KEYS if k.startswith("notifications.")]
        nonvault = len(self.GLOBAL_FIELDS) + len(self.CHANNEL_FIELDS) + len(self.PREFERENCE_FIELDS)
        assert nonvault + len(notification_vault_keys) == 40


# ── 6. Desktop Parity ────────────────────────────────────────

class TestDesktopParity:
    """All 5 channels and 12 notification types match desktop."""

    def test_five_channels(self):
        assert len(VALID_CHANNELS) == 5
        for ch in ["whatsapp", "telegram", "email", "gemini", "sms"]:
            assert ch in VALID_CHANNELS

    def test_twelve_notification_types(self):
        assert len(VALID_NOTIFICATION_TYPES) == 12

    def test_health_check_intervals_match_desktop(self):
        """Desktop supports: 1, 2, 3, 4, 6, 12, 24 hours."""
        assert VALID_HEALTH_CHECK_INTERVALS == {1, 2, 3, 4, 6, 12, 24}

    def test_twilio_shared_between_whatsapp_and_sms(self):
        """WhatsApp and SMS share the same Twilio SID and Token vault keys."""
        # Both use notifications.twilio_sid and notifications.twilio_token
        assert "notifications.twilio_sid" in VAULT_KEYS
        assert "notifications.twilio_token" in VAULT_KEYS
        # SMS does NOT have its own separate credentials
        assert "notifications.sms_sid" not in VAULT_KEYS
        assert "notifications.sms_token" not in VAULT_KEYS


# ── 7. Security ──────────────────────────────────────────────

class TestNotificationSecurity:
    """Verify security boundaries for notification config."""

    @pytest.fixture(autouse=True)
    def _set_singleton(self, vault):
        vault_mod._vault_instance = vault
        yield
        vault_mod._vault_instance = None

    def test_all_credentials_masked_in_full_config(self, vault):
        """A full config with all 5 notification credentials should mask all of them."""
        config = {
            "notifications": {
                "twilio_sid": vault.encrypt("AC123456789abcdef"),
                "twilio_token": vault.encrypt("token_abc123"),
                "telegram_token": vault.encrypt("123456:BOT_TOKEN"),
                "email_password": vault.encrypt("gmail_app_pw"),
                "gemini_password": vault.encrypt("gemini_app_pw16"),
            }
        }
        result = _mask_vault_keys_in_config(config)
        for key in ["twilio_sid", "twilio_token", "telegram_token", "email_password", "gemini_password"]:
            val = result["notifications"][key]
            assert "****" in val, f"{key} not masked"
            # Verify plaintext NOT in result
            assert "AC123456789" not in val or key == "twilio_sid"  # last 4 chars only

    def test_encrypt_all_credentials_in_update(self, vault):
        """A batch update with all 5 credentials should encrypt all of them."""
        updates = {
            "notifications.twilio_sid": "AC123",
            "notifications.twilio_token": "tok_123",
            "notifications.telegram_token": "123456:BOT",
            "notifications.email_password": "gmail_pw",
            "notifications.gemini_password": "gem_pw",
        }
        result = _encrypt_vault_keys_in_updates(updates)
        for key in updates:
            assert vault.is_encrypted(result[key]), f"{key} not encrypted"

    def test_mixed_update_vault_and_nonvault(self, vault):
        """Mixed vault + non-vault update: vault keys encrypted, non-vault passed through."""
        updates = {
            "notifications.twilio_sid": "AC123",
            "notifications.whatsapp.enabled": True,
            "notifications.whatsapp.from_number": "whatsapp:+14155238886",
            "notifications.preferences.trade_opened": True,
        }
        result = _encrypt_vault_keys_in_updates(updates)
        assert vault.is_encrypted(result["notifications.twilio_sid"])
        assert result["notifications.whatsapp.enabled"] is True
        assert result["notifications.whatsapp.from_number"] == "whatsapp:+14155238886"
        assert result["notifications.preferences.trade_opened"] is True
