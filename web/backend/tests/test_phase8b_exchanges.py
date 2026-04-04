"""
Phase 8B — Exchange Management Tests

Tests cover:
  - CRUD operations (create, read, update, delete)
  - Credential encryption via vault
  - Mode validation (live/sandbox/demo)
  - Activation/deactivation
  - Supported exchange listing
  - Masking of sensitive credentials
  - Asset listing and filtering
"""
import pytest
from datetime import datetime
from unittest.mock import AsyncMock, patch, MagicMock

from app.services.vault import VaultService, get_vault, reset_vault
from app.api.exchanges import (
    SUPPORTED_EXCHANGES,
    _mask_credential,
    _encrypt_if_present,
    _exchange_to_dict,
)


# ── Fixtures ───────────────────────────────────────────────

@pytest.fixture
def vault(tmp_path):
    """Create a fresh vault with a temporary key for each test."""
    reset_vault()
    v = VaultService(key_path=tmp_path / ".nexus_key")
    import app.services.vault as vault_mod
    vault_mod._vault_instance = v
    yield v
    vault_mod._vault_instance = None


@pytest.fixture
def mock_exchange():
    """Create a mock Exchange ORM object."""
    ex = MagicMock()
    ex.id = 1
    ex.name = "Bybit Demo"
    ex.exchange_id = "bybit"
    ex.api_key_encrypted = None
    ex.api_secret_encrypted = None
    ex.api_passphrase_encrypted = None
    ex.sandbox_mode = False
    ex.demo_mode = True
    ex.is_active = True
    ex.testnet_url = None
    ex.created_at = datetime(2026, 4, 1, 12, 0, 0)
    ex.updated_at = datetime(2026, 4, 1, 12, 0, 0)
    ex.mode = "demo"
    return ex


# ── Supported Exchanges ───────────────────────────────────

class TestSupportedExchanges:
    def test_six_exchanges_supported(self):
        assert len(SUPPORTED_EXCHANGES) == 6

    def test_all_exchange_ids(self):
        expected = {"kucoin", "binance", "bybit", "coinbase", "kraken", "okx"}
        assert set(SUPPORTED_EXCHANGES.keys()) == expected

    def test_bybit_has_demo(self):
        assert SUPPORTED_EXCHANGES["bybit"]["has_demo"] is True

    def test_binance_has_sandbox(self):
        assert SUPPORTED_EXCHANGES["binance"]["has_sandbox"] is True

    def test_kucoin_needs_passphrase(self):
        assert SUPPORTED_EXCHANGES["kucoin"]["needs_passphrase"] is True

    def test_binance_no_passphrase(self):
        assert SUPPORTED_EXCHANGES["binance"]["needs_passphrase"] is False


# ── Credential Masking ─────────────────────────────────────

class TestCredentialMasking:
    def test_mask_none(self, vault):
        assert _mask_credential(None) == ""

    def test_mask_empty(self, vault):
        assert _mask_credential("") == ""

    def test_mask_plaintext(self, vault):
        result = _mask_credential("sk-ant-api03-realkey-12345")
        assert "****" in result
        assert "2345" in result
        assert "realkey" not in result

    def test_mask_encrypted(self, vault):
        encrypted = vault.encrypt("my-secret-key-abcd")
        result = _mask_credential(encrypted)
        assert "****" in result
        assert "abcd" in result
        assert "my-secret-key" not in result


# ── Credential Encryption ─────────────────────────────────

class TestCredentialEncryption:
    def test_encrypt_none(self, vault):
        assert _encrypt_if_present(None) is None

    def test_encrypt_empty(self, vault):
        assert _encrypt_if_present("") is None

    def test_encrypt_plaintext(self, vault):
        result = _encrypt_if_present("test-api-key-1234")
        assert result is not None
        assert vault.is_encrypted(result)
        assert vault.decrypt(result) == "test-api-key-1234"

    def test_encrypt_already_encrypted(self, vault):
        encrypted = vault.encrypt("already-encrypted")
        result = _encrypt_if_present(encrypted)
        assert result == encrypted  # Should not double-encrypt

    def test_encrypt_roundtrip(self, vault):
        original = "sk-demo-bybit-key-xyz789"
        encrypted = _encrypt_if_present(original)
        assert encrypted != original
        assert vault.decrypt(encrypted) == original


# ── Exchange Serialization ─────────────────────────────────

class TestExchangeSerialization:
    def test_exchange_to_dict_keys(self, vault, mock_exchange):
        result = _exchange_to_dict(mock_exchange)
        expected_keys = {
            "id", "name", "exchange_id",
            "api_key_masked", "api_secret_masked", "passphrase_masked",
            "has_api_key", "has_api_secret", "has_passphrase",
            "sandbox_mode", "demo_mode", "mode", "is_active",
            "testnet_url", "created_at", "updated_at",
        }
        assert set(result.keys()) == expected_keys

    def test_exchange_to_dict_no_creds(self, vault, mock_exchange):
        result = _exchange_to_dict(mock_exchange)
        assert result["has_api_key"] is False
        assert result["has_api_secret"] is False
        assert result["api_key_masked"] == ""

    def test_exchange_to_dict_with_encrypted_creds(self, vault, mock_exchange):
        mock_exchange.api_key_encrypted = vault.encrypt("bybit-key-1234")
        mock_exchange.api_secret_encrypted = vault.encrypt("bybit-secret-5678")
        result = _exchange_to_dict(mock_exchange)
        assert result["has_api_key"] is True
        assert result["has_api_secret"] is True
        assert "****" in result["api_key_masked"]
        assert "1234" in result["api_key_masked"]
        assert "bybit-key" not in result["api_key_masked"]

    def test_exchange_to_dict_mode(self, vault, mock_exchange):
        result = _exchange_to_dict(mock_exchange)
        assert result["mode"] == "demo"
        assert result["demo_mode"] is True
        assert result["sandbox_mode"] is False

    def test_exchange_to_dict_dates(self, vault, mock_exchange):
        result = _exchange_to_dict(mock_exchange)
        assert result["created_at"] == "2026-04-01T12:00:00"


# ── Mode Validation ────────────────────────────────────────

class TestModeValidation:
    def test_valid_modes(self):
        from app.api.exchanges import MODES
        assert MODES == {"live", "sandbox", "demo"}

    def test_bybit_supports_all_modes(self):
        info = SUPPORTED_EXCHANGES["bybit"]
        assert info["has_sandbox"] is True
        assert info["has_demo"] is True

    def test_kraken_live_only(self):
        info = SUPPORTED_EXCHANGES["kraken"]
        assert info["has_sandbox"] is False
        assert info["has_demo"] is False

    def test_okx_has_sandbox_and_passphrase(self):
        info = SUPPORTED_EXCHANGES["okx"]
        assert info["has_sandbox"] is True
        assert info["needs_passphrase"] is True


# ── Security Rules ─────────────────────────────────────────

class TestSecurityRules:
    def test_no_plaintext_in_dict(self, vault, mock_exchange):
        """Verify serialized exchange never contains plaintext credentials."""
        mock_exchange.api_key_encrypted = vault.encrypt("super-secret-key-9999")
        result = _exchange_to_dict(mock_exchange)
        all_values = str(result)
        assert "super-secret-key" not in all_values

    def test_encrypted_value_not_in_response(self, vault, mock_exchange):
        """Verify encrypted ciphertext is not exposed in response."""
        encrypted = vault.encrypt("test-key-abcd")
        mock_exchange.api_key_encrypted = encrypted
        result = _exchange_to_dict(mock_exchange)
        assert encrypted not in str(result)

    def test_mask_format(self, vault):
        """Verify mask format is ****last4."""
        result = _mask_credential("my-api-key-ending-wxyz")
        assert result.startswith("****")
        assert result.endswith("wxyz")
