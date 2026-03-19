"""
tests/unit/test_keyvault.py — KeyVault security tests (KV-001 to KV-007)

All tests use a fully isolated KeyVault pointed at a pytest tmp_path so
the real .nexus_vault.json and .nexus_key on disk are never touched.
"""

import json
import logging
from pathlib import Path

import pytest

from core.security.key_vault import KeyVault


# ── shared fixture: isolated KeyVault ────────────────────────────────────────

@pytest.fixture
def vault(tmp_path):
    """
    Fresh KeyVault instance with its vault file and key file
    living inside a pytest-managed temporary directory.
    Nothing written here touches the real data/ directory.
    """
    kv = KeyVault()
    kv._vault_path = tmp_path / ".nexus_vault.json"
    kv._key_path   = tmp_path / ".nexus_key"
    return kv


# ══════════════════════════════════════════════════════════════════════════════
#  KV-001 — store and retrieve round-trip
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_kv001_store_and_retrieve(vault):
    """
    Saving a value and loading it back must return the exact original string.
    This validates the full encrypt → persist → load → decrypt cycle.
    """
    vault.save("test.api_key", "ABC123")
    result = vault.load("test.api_key")
    assert result == "ABC123", (
        f"Round-trip failed: expected 'ABC123', got {result!r}"
    )


@pytest.mark.unit
def test_kv001_multiple_keys_independent(vault):
    """
    Storing two different keys must not corrupt each other's values.
    """
    vault.save("service.key_a", "VALUE_A")
    vault.save("service.key_b", "VALUE_B")

    assert vault.load("service.key_a") == "VALUE_A"
    assert vault.load("service.key_b") == "VALUE_B"


# ══════════════════════════════════════════════════════════════════════════════
#  KV-002 — value is encrypted at rest
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_kv002_value_encrypted_at_rest(vault):
    """
    The raw bytes of the vault JSON file must NOT contain the plaintext value.
    If the ciphertext in the file equals the plaintext, Fernet encryption is broken.
    """
    secret = "SUPER_SECRET_KEY_XYZ"
    vault.save("my.secret", secret)

    raw = vault._vault_path.read_text(encoding="utf-8")

    assert secret not in raw, (
        "Plaintext secret found in vault file — encryption is not working!"
    )


@pytest.mark.unit
def test_kv002_stored_value_is_fernet_token(vault):
    """
    The stored ciphertext must be a valid Fernet token (base64url, starts with 'gAAA').
    This confirms Fernet (not some other scheme) is being used.
    """
    vault.save("my.secret", "ANY_VALUE")

    raw_dict = json.loads(vault._vault_path.read_text(encoding="utf-8"))
    ciphertext = raw_dict["my.secret"]

    # Fernet tokens are base64url-encoded and always start with 'gAAA'
    assert ciphertext.startswith("gAAA"), (
        f"Stored value does not look like a Fernet token: {ciphertext[:30]!r}"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  KV-003 — wrong encryption key returns ciphertext, not original plaintext
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_kv003_wrong_key_does_not_return_plaintext(tmp_path):
    """
    If the .nexus_key is replaced with a different Fernet key after a value
    was saved, load() must NOT return the original plaintext.

    KeyVault._decrypt() catches InvalidToken and returns the raw ciphertext
    instead of raising — so we assert that the returned value is not the
    original secret (it will be the raw encrypted string).
    """
    from cryptography.fernet import Fernet

    # ── Step 1: create vault, save a secret with key A ──────────
    vault_a = KeyVault()
    vault_a._vault_path = tmp_path / ".nexus_vault.json"
    vault_a._key_path   = tmp_path / ".nexus_key"

    vault_a.save("api.key", "ORIGINAL_SECRET")
    assert vault_a.load("api.key") == "ORIGINAL_SECRET"  # sanity check

    # ── Step 2: overwrite .nexus_key with a completely different key ──
    new_key = Fernet.generate_key()
    (tmp_path / ".nexus_key").write_bytes(new_key)

    # ── Step 3: a fresh vault instance reads the tampered key ────
    vault_b = KeyVault()
    vault_b._vault_path = tmp_path / ".nexus_vault.json"
    vault_b._key_path   = tmp_path / ".nexus_key"

    result = vault_b.load("api.key")

    assert result != "ORIGINAL_SECRET", (
        "Wrong key returned the original plaintext — decryption is not key-bound!"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  KV-004 — missing key returns empty string, no exception
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_kv004_missing_key_returns_empty_string(vault):
    """
    Loading a key that was never stored must return "" without raising.
    """
    result = vault.load("nonexistent.key")
    assert result == "", f"Expected '', got {result!r}"


@pytest.mark.unit
def test_kv004_has_key_returns_false_for_missing(vault):
    """
    has_key() must return False for a key that was never stored.
    """
    assert vault.has_key("this.key.does.not.exist") is False


@pytest.mark.unit
def test_kv004_empty_string_save_deletes_key(vault):
    """
    Saving an empty string is the documented way to delete a key.
    After saving "" the key must no longer be retrievable.
    """
    vault.save("temp.key", "SOME_VALUE")
    assert vault.load("temp.key") == "SOME_VALUE"  # confirm it was set

    vault.save("temp.key", "")                      # delete via empty string
    assert vault.load("temp.key") == ""
    assert vault.has_key("temp.key") is False


# ══════════════════════════════════════════════════════════════════════════════
#  KV-005 — API key never appears in log output
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_kv005_key_value_not_in_debug_logs(vault, caplog):
    """
    With DEBUG logging enabled the actual secret value must never appear
    in any log record emitted during save() or load().

    This protects against accidental log-level exposure of credentials.
    """
    secret = "VERY_SECRET_API_KEY_DO_NOT_LOG"

    with caplog.at_level(logging.DEBUG, logger="core.security.key_vault"):
        vault.save("api.secret", secret)
        _ = vault.load("api.secret")

    all_log_text = " ".join(r.getMessage() for r in caplog.records)

    assert secret not in all_log_text, (
        f"Secret value appeared in log output!\n"
        f"Log content: {all_log_text[:500]}"
    )


@pytest.mark.unit
def test_kv005_key_name_may_appear_but_not_value(vault, caplog):
    """
    The KEY NAME (e.g. 'ai.openai_api_key') may appear in logs for traceability,
    but the VALUE must never appear at any log level.
    """
    key_name  = "ai.openai_api_key"
    key_value = "sk-REAL_SECRET_VALUE_abc123"

    with caplog.at_level(logging.DEBUG):
        vault.save(key_name, key_value)
        vault.load(key_name)

    all_log_text = " ".join(r.getMessage() for r in caplog.records)

    # Value must be absent
    assert key_value not in all_log_text, (
        "API key value leaked into log output!"
    )
    # Key name appearing is acceptable (and expected at DEBUG level)
    # — no assertion required for the name


# ══════════════════════════════════════════════════════════════════════════════
#  KV-006 — vault file created on first use
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_kv006_vault_file_created_on_first_save(vault):
    """
    Before any save() the vault file must not exist.
    After the first save() it must exist and be valid JSON.
    """
    assert not vault._vault_path.exists(), (
        "Vault file exists before any save() call — unexpected pre-creation."
    )

    vault.save("first.key", "first_value")

    assert vault._vault_path.exists(), (
        "Vault file was not created after save()."
    )

    # Must be valid JSON
    content = json.loads(vault._vault_path.read_text(encoding="utf-8"))
    assert isinstance(content, dict), "Vault file content is not a JSON object."
    assert "first.key" in content, "Saved key not present in vault file."


@pytest.mark.unit
def test_kv006_key_file_created_on_first_save(vault):
    """
    The Fernet key file (.nexus_key) must be created on the first save()
    if it does not already exist.
    """
    assert not vault._key_path.exists(), (
        "Key file exists before any operation — unexpected pre-creation."
    )

    vault.save("any.key", "any_value")

    assert vault._key_path.exists(), (
        ".nexus_key was not created after the first save()."
    )

    # Key file must be non-empty (44 bytes is the standard Fernet key size in base64)
    key_bytes = vault._key_path.read_bytes()
    assert len(key_bytes) >= 44, (
        f"Key file is too short ({len(key_bytes)} bytes) to be a valid Fernet key."
    )


# ══════════════════════════════════════════════════════════════════════════════
#  KV-007 — Unicode values handled correctly
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_kv007_unicode_round_trip(vault):
    """
    Values containing non-ASCII characters (accented letters, CJK, emoji,
    special punctuation) must survive a save → load round-trip intact.
    """
    unicode_values = [
        "sk-àáâãäåæ",              # Latin extended
        "密钥_1234567890",           # CJK characters
        "ключ_секрет",              # Cyrillic
        "مفتاح_سري",               # Arabic
        "🔑💡🚀 rocket-key",         # Emoji
        "API\u2019s «key» — v2",    # Smart quotes, em-dash, guillemets
    ]

    for i, value in enumerate(unicode_values):
        key_name = f"unicode.test.{i}"
        vault.save(key_name, value)
        result = vault.load(key_name)
        assert result == value, (
            f"Unicode round-trip failed for value {value!r}: "
            f"got {result!r}"
        )


@pytest.mark.unit
def test_kv007_unicode_not_in_vault_file_raw(vault):
    """
    Even when the plaintext contains non-ASCII characters, the vault file
    should store only the Fernet ciphertext — the original Unicode string
    must not appear in the raw JSON.
    """
    unicode_secret = "密钥_SUPER_SECRET"
    vault.save("unicode.secret", unicode_secret)

    raw = vault._vault_path.read_text(encoding="utf-8")

    assert unicode_secret not in raw, (
        "Unicode plaintext found unencrypted in vault file!"
    )
