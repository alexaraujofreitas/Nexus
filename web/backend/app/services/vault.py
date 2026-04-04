# ============================================================
# NEXUS TRADER Web — Vault Encryption Service
#
# Fernet AES-256 encryption for all sensitive configuration
# values (API keys, secrets, tokens, passwords).
#
# Mirrors the desktop key_vault.py encryption pattern.
# ============================================================
from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


class VaultDecryptionError(Exception):
    """Raised when decryption fails (invalid key or corrupted data)."""


class VaultService:
    """
    Fernet AES-256 vault for encrypting sensitive configuration values.

    Key is loaded from:
    1. NEXUS_ENCRYPTION_KEY environment variable (preferred for Docker/K8s), or
    2. A file at ``key_path`` (auto-generated on first run).

    All encrypt/decrypt operations are synchronous (Fernet is CPU-bound,
    not I/O-bound, and completes in microseconds).
    """

    # Registry of config keys that MUST be encrypted
    VAULT_KEYS: frozenset[str] = frozenset({
        # AI provider keys
        "ai.anthropic_api_key",
        "ai.openai_api_key",
        "ai.gemini_api_key",
        # Sentiment / data feed keys
        "sentiment.news_api_key",
        "sentiment.reddit_client_id",
        "sentiment.reddit_client_secret",
        "sentiment.cryptopanic_api_key",
        # Agent API keys
        "agents.fred_api_key",
        "agents.lunarcrush_api_key",
        "agents.coinglass_api_key",
        "agents.cryptopanic_api_key",
        # Notification credentials
        "notifications.twilio_sid",
        "notifications.twilio_token",
        "notifications.telegram_token",
        "notifications.email_password",
        "notifications.gemini_password",
        # Exchange credentials (handled separately via exchanges table,
        # but included here for completeness)
        "exchange.api_key",
        "exchange.api_secret",
        "exchange.api_passphrase",
        # API keys stored in settings
        "api_keys.cryptopanic",
        "api_keys.coinglass",
        "api_keys.reddit_client_id",
        "api_keys.reddit_client_secret",
    })

    def __init__(self, key_path: Optional[Path] = None):
        """
        Initialize the vault.

        Parameters
        ----------
        key_path : Path, optional
            Path to the Fernet key file. Defaults to ``data/.nexus_web_key``
            relative to the current working directory.
        """
        self._key_path = key_path or Path("data/.nexus_web_key")
        self._fernet: Fernet = self._load_or_create_key()

    def _load_or_create_key(self) -> Fernet:
        """Load key from env var or file; generate file if neither exists."""
        # Priority 1: Environment variable
        env_key = os.environ.get("NEXUS_ENCRYPTION_KEY", "").strip()
        if env_key:
            logger.info("Vault: using encryption key from NEXUS_ENCRYPTION_KEY env var")
            try:
                return Fernet(env_key.encode())
            except Exception as exc:
                logger.error("Vault: invalid NEXUS_ENCRYPTION_KEY — %s", exc)
                raise ValueError(
                    "NEXUS_ENCRYPTION_KEY is not a valid Fernet key"
                ) from exc

        # Priority 2: Key file
        if self._key_path.exists():
            logger.info("Vault: loading key from %s", self._key_path)
            raw = self._key_path.read_bytes().strip()
            try:
                return Fernet(raw)
            except Exception as exc:
                logger.error("Vault: corrupt key file %s — %s", self._key_path, exc)
                raise ValueError(
                    f"Vault key file {self._key_path} is corrupt"
                ) from exc

        # Priority 3: Generate new key
        logger.info("Vault: generating new encryption key at %s", self._key_path)
        self._key_path.parent.mkdir(parents=True, exist_ok=True)
        new_key = Fernet.generate_key()
        self._key_path.write_bytes(new_key)
        # Set file permissions to owner-only (0o600)
        try:
            os.chmod(self._key_path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            # On Windows, chmod may not be fully supported
            logger.warning("Vault: could not set key file permissions to 0o600")
        return Fernet(new_key)

    # ── Core Operations ────────────────────────────────────────

    def encrypt(self, plaintext: str) -> str:
        """
        Encrypt a plaintext string.

        Returns base64-encoded Fernet token as a string.
        Empty strings are returned as-is (nothing to protect).
        """
        if not plaintext:
            return ""
        token = self._fernet.encrypt(plaintext.encode("utf-8"))
        return token.decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        """
        Decrypt a Fernet-encrypted string.

        Returns the original plaintext.
        Raises ``VaultDecryptionError`` on failure.
        """
        if not ciphertext:
            return ""
        try:
            plaintext = self._fernet.decrypt(ciphertext.encode("ascii"))
            return plaintext.decode("utf-8")
        except InvalidToken as exc:
            raise VaultDecryptionError(
                "Decryption failed — wrong key or corrupted data"
            ) from exc
        except Exception as exc:
            raise VaultDecryptionError(f"Decryption error: {exc}") from exc

    def is_encrypted(self, value: str) -> bool:
        """
        Check if a value looks like a Fernet token.

        Fernet tokens are base64url-encoded and start with 'gAAAAA'.
        """
        if not value:
            return False
        return value.startswith("gAAAAA") and len(value) > 50

    # ── Masking ────────────────────────────────────────────────

    @staticmethod
    def mask(value: str) -> str:
        """
        Mask a sensitive value for display: ``****last4``.

        Returns empty string for empty/None values.
        Returns ``****`` for values shorter than 4 characters.
        """
        if not value:
            return ""
        if len(value) <= 4:
            return "****"
        return f"****{value[-4:]}"

    # ── Key Rotation ───────────────────────────────────────────

    def rotate_key(self, encrypted_values: dict[str, str]) -> tuple[dict[str, str], str]:
        """
        Rotate the encryption key and re-encrypt all provided values.

        Parameters
        ----------
        encrypted_values : dict
            Mapping of {config_key: encrypted_value} to re-encrypt.

        Returns
        -------
        tuple[dict, str]
            (re-encrypted_values, new_key_b64) — the new key as base64 string.
        """
        # Decrypt all values with current key
        decrypted = {}
        for key, enc_val in encrypted_values.items():
            if enc_val:
                try:
                    decrypted[key] = self.decrypt(enc_val)
                except VaultDecryptionError:
                    logger.warning("Vault rotate: could not decrypt key=%s, skipping", key)
                    decrypted[key] = enc_val  # leave as-is
            else:
                decrypted[key] = ""

        # Generate new key
        new_key = Fernet.generate_key()
        new_fernet = Fernet(new_key)

        # Re-encrypt all values with new key
        re_encrypted = {}
        for key, plain in decrypted.items():
            if plain and not self.is_encrypted(plain):
                re_encrypted[key] = new_fernet.encrypt(plain.encode("utf-8")).decode("ascii")
            else:
                re_encrypted[key] = plain

        # Persist new key
        if self._key_path and not os.environ.get("NEXUS_ENCRYPTION_KEY"):
            self._key_path.write_bytes(new_key)
            try:
                os.chmod(self._key_path, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass

        # Update internal state
        self._fernet = new_fernet

        return re_encrypted, new_key.decode("ascii")

    # ── Utility ────────────────────────────────────────────────

    @classmethod
    def is_vault_key(cls, config_key: str) -> bool:
        """Check whether a config key should be vault-encrypted."""
        return config_key in cls.VAULT_KEYS

    @property
    def key_file_exists(self) -> bool:
        return self._key_path.exists()

    @property
    def key_source(self) -> str:
        if os.environ.get("NEXUS_ENCRYPTION_KEY"):
            return "environment"
        if self._key_path.exists():
            return "file"
        return "none"

    def status(self) -> dict:
        """Return vault status info (no secrets)."""
        return {
            "key_source": self.key_source,
            "key_file_exists": self.key_file_exists,
            "key_file_path": str(self._key_path),
            "vault_keys_count": len(self.VAULT_KEYS),
        }


# ── Singleton ──────────────────────────────────────────────
_vault_instance: VaultService | None = None


def get_vault(key_path: Optional[Path] = None) -> VaultService:
    """Return the global VaultService singleton."""
    global _vault_instance
    if _vault_instance is None:
        _vault_instance = VaultService(key_path=key_path)
    return _vault_instance


def reset_vault() -> None:
    """Reset the singleton (for testing)."""
    global _vault_instance
    _vault_instance = None
