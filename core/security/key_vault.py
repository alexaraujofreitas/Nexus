# ============================================================
# NEXUS TRADER — API Key Vault  (Phase D3)
#
# Stores sensitive API keys (AI providers, sentiment APIs)
# as Fernet-encrypted values in a local JSON vault file.
#
# Uses the SAME Fernet key as exchange_page.py so a single
# `.nexus_key` protects all secrets on the device.
#
# Vault file: DATA_DIR/.nexus_vault.json
#   {"ai.anthropic_api_key": "<fernet-ciphertext>", ...}
#
# Usage:
#   from core.security.key_vault import key_vault
#   key_vault.save("ai.anthropic_api_key", "sk-ant-...")
#   key_vault.load("ai.anthropic_api_key")   # → "sk-ant-..."
# ============================================================
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Key names that are vault-managed (never written to YAML) ──
VAULT_KEYS: frozenset[str] = frozenset({
    "ai.anthropic_api_key",
    "ai.openai_api_key",
    "ai.gemini_api_key",
    "sentiment.news_api_key",
    "sentiment.reddit_client_id",
    "sentiment.reddit_client_secret",
})


class KeyVault:
    """
    Thread-safe local API key vault using Fernet symmetric encryption.

    Reads / writes DATA_DIR/.nexus_vault.json.
    Falls back to plaintext round-trip if the cryptography package is
    unavailable (non-blocking degradation).
    """

    def __init__(self):
        self._lock   = threading.RLock()
        self._vault  = None   # loaded lazily
        self._vault_path: Optional[Path] = None
        self._key_path:   Optional[Path] = None

    # ── Internal helpers ──────────────────────────────────────

    def _init_paths(self):
        """Resolve DATA_DIR-based paths on first use (avoids import-time side-effects)."""
        if self._vault_path is not None:
            return
        from config.constants import DATA_DIR
        self._vault_path = DATA_DIR / ".nexus_vault.json"
        self._key_path   = DATA_DIR / ".nexus_key"

    def _load_vault(self) -> dict:
        """Read and return the raw (encrypted) vault dict from disk."""
        self._init_paths()
        if not self._vault_path.exists():
            return {}
        try:
            return json.loads(self._vault_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("KeyVault: could not read vault file: %s", exc)
            return {}

    def _save_vault(self, vault: dict):
        """Persist the raw (encrypted) vault dict to disk."""
        self._init_paths()
        try:
            self._vault_path.parent.mkdir(parents=True, exist_ok=True)
            self._vault_path.write_text(
                json.dumps(vault, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            # Restrict permissions (best-effort on Windows too)
            try:
                self._vault_path.chmod(0o600)
            except Exception:
                pass
        except Exception as exc:
            logger.error("KeyVault: could not write vault file: %s", exc)

    def _fernet(self):
        """Return a Fernet instance using the shared .nexus_key."""
        self._init_paths()
        try:
            from cryptography.fernet import Fernet
            if not self._key_path.exists():
                key = Fernet.generate_key()
                self._key_path.write_bytes(key)
                try:
                    self._key_path.chmod(0o600)
                except Exception:
                    pass
            return Fernet(self._key_path.read_bytes())
        except Exception as exc:
            logger.warning("KeyVault: Fernet unavailable (%s) — storing keys unencrypted", exc)
            return None

    def _encrypt(self, plaintext: str) -> str:
        f = self._fernet()
        if f is None:
            return plaintext
        return f.encrypt(plaintext.encode()).decode()

    def _decrypt(self, ciphertext: str) -> str:
        f = self._fernet()
        if f is None:
            return ciphertext
        try:
            return f.decrypt(ciphertext.encode()).decode()
        except Exception:
            return ciphertext   # already plaintext (migration from pre-vault)

    # ── Public API ────────────────────────────────────────────

    def save(self, name: str, plaintext: str):
        """
        Encrypt and persist a key value.
        Passing an empty string is equivalent to deleting the key.
        """
        with self._lock:
            vault = self._load_vault()
            if plaintext:
                vault[name] = self._encrypt(plaintext)
            elif name in vault:
                del vault[name]
            self._save_vault(vault)
            logger.debug("KeyVault: saved key '%s'", name)

    def load(self, name: str) -> str:
        """
        Return the decrypted value for *name*, or "" if not found.
        """
        with self._lock:
            vault = self._load_vault()
            ciphertext = vault.get(name, "")
            if not ciphertext:
                return ""
            return self._decrypt(ciphertext)

    def has_key(self, name: str) -> bool:
        """Return True if the vault has a non-empty entry for *name*."""
        with self._lock:
            return bool(self._load_vault().get(name, ""))

    def delete(self, name: str):
        """Remove *name* from the vault."""
        with self._lock:
            vault = self._load_vault()
            if name in vault:
                del vault[name]
                self._save_vault(vault)

    def migrate_from_settings(self):
        """
        One-time migration: if AI / sentiment keys are found in settings.yaml
        (pre-D3 installation), move them to the vault and clear them from YAML.
        Called at startup from AppSettings.load().
        """
        try:
            from config.settings import settings as _s
            for key in VAULT_KEYS:
                yaml_val = _s.get(key, "").strip()
                if yaml_val and not yaml_val.startswith("__vault__"):
                    if not self.has_key(key):
                        logger.info("KeyVault.migrate: moving '%s' to vault", key)
                        self.save(key, yaml_val)
                    # Erase from YAML regardless
                    _s.set(key, "__vault__")
        except ImportError as exc:
            # config.settings may not be fully initialized yet (circular import
            # at startup). This is expected — migration will be retried on next run.
            logger.debug("KeyVault.migrate: deferred — %s", exc)
        except Exception as exc:
            logger.debug("KeyVault.migrate: %s", exc)


# Global singleton
key_vault = KeyVault()
