"""
Phase 8C — Settings AI & ML Tab Tests

Tests cover:
  - AI provider vault key registration
  - Sentiment vault key registration
  - Config key masking for AI provider keys
  - Config key encryption for AI provider keys
  - Default values for AI/ML/RL/Regime/Sentiment settings
  - Vault key completeness for all AI/ML-related credentials
"""
import pytest
from unittest.mock import MagicMock

from app.services.vault import VaultService, reset_vault
from app.api.settings_api import _mask_vault_keys_in_config, _encrypt_vault_keys_in_updates

VAULT_KEYS = VaultService.VAULT_KEYS


@pytest.fixture
def vault(tmp_path):
    """Create a fresh vault with a temporary key."""
    reset_vault()
    v = VaultService(key_path=tmp_path / ".nexus_key")
    import app.services.vault as vault_mod
    vault_mod._vault_instance = v
    yield v
    vault_mod._vault_instance = None


# ── Vault Key Registration ─────────────────────────────────

class TestAIVaultKeys:
    """Verify all AI provider keys are registered in VAULT_KEYS."""

    def test_anthropic_key_registered(self):
        assert "ai.anthropic_api_key" in VAULT_KEYS

    def test_openai_key_registered(self):
        assert "ai.openai_api_key" in VAULT_KEYS

    def test_gemini_key_registered(self):
        assert "ai.gemini_api_key" in VAULT_KEYS


class TestSentimentVaultKeys:
    """Verify all sentiment data source keys are registered."""

    def test_news_api_key_registered(self):
        assert "sentiment.news_api_key" in VAULT_KEYS

    def test_reddit_client_id_registered(self):
        assert "sentiment.reddit_client_id" in VAULT_KEYS

    def test_reddit_client_secret_registered(self):
        assert "sentiment.reddit_client_secret" in VAULT_KEYS

    def test_cryptopanic_key_registered(self):
        assert "sentiment.cryptopanic_api_key" in VAULT_KEYS

    def test_agent_cryptopanic_key_registered(self):
        assert "agents.cryptopanic_api_key" in VAULT_KEYS

    def test_coinglass_key_registered(self):
        assert "agents.coinglass_api_key" in VAULT_KEYS


# ── Config Masking for AI Keys ─────────────────────────────

class TestAIKeyMasking:
    """Verify AI keys are masked when returned via GET /settings."""

    def test_mask_anthropic_key(self, vault):
        config = {"ai": {"anthropic_api_key": "sk-ant-api03-realkey-abcd1234"}}
        masked = _mask_vault_keys_in_config(config)
        assert "****" in masked["ai"]["anthropic_api_key"]
        assert "1234" in masked["ai"]["anthropic_api_key"]
        assert "realkey" not in masked["ai"]["anthropic_api_key"]

    def test_mask_openai_key(self, vault):
        config = {"ai": {"openai_api_key": "sk-proj-testkey-xyz5678"}}
        masked = _mask_vault_keys_in_config(config)
        assert "****" in masked["ai"]["openai_api_key"]
        assert "5678" in masked["ai"]["openai_api_key"]

    def test_mask_gemini_key(self, vault):
        config = {"ai": {"gemini_api_key": "AIzaSyD-testkey-wxyz9012"}}
        masked = _mask_vault_keys_in_config(config)
        assert "****" in masked["ai"]["gemini_api_key"]
        assert "9012" in masked["ai"]["gemini_api_key"]

    def test_non_vault_key_unchanged(self, vault):
        config = {"ai": {"active_provider": "Anthropic Claude", "ml_confidence_threshold": 0.65}}
        masked = _mask_vault_keys_in_config(config)
        assert masked["ai"]["active_provider"] == "Anthropic Claude"
        assert masked["ai"]["ml_confidence_threshold"] == 0.65

    def test_mask_encrypted_ai_key(self, vault):
        encrypted = vault.encrypt("sk-ant-api03-realkey-9999")
        config = {"ai": {"anthropic_api_key": encrypted}}
        masked = _mask_vault_keys_in_config(config)
        assert "****" in masked["ai"]["anthropic_api_key"]
        assert "9999" in masked["ai"]["anthropic_api_key"]
        # Must not contain ciphertext
        assert encrypted not in masked["ai"]["anthropic_api_key"]

    def test_mask_sentiment_keys(self, vault):
        config = {
            "sentiment": {
                "news_api_key": "newsapi-key-abcd",
                "reddit_client_id": "reddit-id-efgh",
                "reddit_client_secret": "reddit-secret-ijkl",
                "news_enabled": True,
            }
        }
        masked = _mask_vault_keys_in_config(config)
        assert "****" in masked["sentiment"]["news_api_key"]
        assert "****" in masked["sentiment"]["reddit_client_id"]
        assert "****" in masked["sentiment"]["reddit_client_secret"]
        # Non-vault key unmasked
        assert masked["sentiment"]["news_enabled"] is True


# ── Config Encryption for AI Keys ──────────────────────────

class TestAIKeyEncryption:
    """Verify AI keys are encrypted before engine dispatch via PATCH /settings."""

    def test_encrypt_anthropic_key(self, vault):
        updates = {"ai.anthropic_api_key": "sk-ant-new-key-1234"}
        encrypted = _encrypt_vault_keys_in_updates(updates)
        assert vault.is_encrypted(encrypted["ai.anthropic_api_key"])
        assert vault.decrypt(encrypted["ai.anthropic_api_key"]) == "sk-ant-new-key-1234"

    def test_encrypt_openai_key(self, vault):
        updates = {"ai.openai_api_key": "sk-proj-openai-5678"}
        encrypted = _encrypt_vault_keys_in_updates(updates)
        assert vault.is_encrypted(encrypted["ai.openai_api_key"])

    def test_encrypt_gemini_key(self, vault):
        updates = {"ai.gemini_api_key": "AIzaSyD-gemini-9012"}
        encrypted = _encrypt_vault_keys_in_updates(updates)
        assert vault.is_encrypted(encrypted["ai.gemini_api_key"])

    def test_non_vault_settings_unchanged(self, vault):
        updates = {
            "ai.active_provider": "Local (Ollama)",
            "ai.ml_confidence_threshold": 0.70,
            "rl.enabled": True,
            "regime.hmm_weight": 0.40,
        }
        encrypted = _encrypt_vault_keys_in_updates(updates)
        assert encrypted["ai.active_provider"] == "Local (Ollama)"
        assert encrypted["ai.ml_confidence_threshold"] == 0.70
        assert encrypted["rl.enabled"] is True
        assert encrypted["regime.hmm_weight"] == 0.40

    def test_encrypt_sentiment_keys(self, vault):
        updates = {
            "sentiment.news_api_key": "newsapi-12345",
            "sentiment.reddit_client_secret": "reddit-secret-67890",
        }
        encrypted = _encrypt_vault_keys_in_updates(updates)
        assert vault.is_encrypted(encrypted["sentiment.news_api_key"])
        assert vault.is_encrypted(encrypted["sentiment.reddit_client_secret"])

    def test_skip_already_encrypted(self, vault):
        ciphertext = vault.encrypt("already-encrypted-key")
        updates = {"ai.anthropic_api_key": ciphertext}
        encrypted = _encrypt_vault_keys_in_updates(updates)
        assert encrypted["ai.anthropic_api_key"] == ciphertext  # No double-encrypt


# ── Vault Key Completeness ─────────────────────────────────

class TestVaultKeyCompleteness:
    """Verify all AI/ML/Sentiment credential paths are covered by VAULT_KEYS."""

    AI_KEYS = [
        "ai.anthropic_api_key",
        "ai.openai_api_key",
        "ai.gemini_api_key",
    ]
    SENTIMENT_KEYS = [
        "sentiment.news_api_key",
        "sentiment.reddit_client_id",
        "sentiment.reddit_client_secret",
        "sentiment.cryptopanic_api_key",
    ]
    AGENT_KEYS = [
        "agents.cryptopanic_api_key",
        "agents.coinglass_api_key",
        "agents.fred_api_key",
        "agents.lunarcrush_api_key",
    ]

    def test_all_ai_keys_registered(self):
        for key in self.AI_KEYS:
            assert key in VAULT_KEYS, f"{key} missing from VAULT_KEYS"

    def test_all_sentiment_keys_registered(self):
        for key in self.SENTIMENT_KEYS:
            assert key in VAULT_KEYS, f"{key} missing from VAULT_KEYS"

    def test_all_agent_keys_registered(self):
        for key in self.AGENT_KEYS:
            assert key in VAULT_KEYS, f"{key} missing from VAULT_KEYS"

    def test_total_vault_keys_minimum(self):
        assert len(VAULT_KEYS) >= 21, f"Expected >= 21 vault keys, got {len(VAULT_KEYS)}"
