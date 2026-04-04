# ============================================================
# NEXUS TRADER Web — Vault Router
#
# GET  /vault/status       — vault key status
# POST /vault/rotate-key   — rotate encryption key
# ============================================================
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.dependencies import get_current_user
from app.services.vault import get_vault, VaultDecryptionError
from app.api.engine import _send_engine_command

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/vault",
    tags=["vault"],
    dependencies=[Depends(get_current_user)],
)


class RotateKeyResponse(BaseModel):
    status: str
    re_encrypted_count: int
    message: str


@router.get("/status")
async def vault_status():
    """Get vault status (key source, file existence, key count)."""
    vault = get_vault()
    return vault.status()


@router.post("/rotate-key", response_model=RotateKeyResponse)
async def rotate_key():
    """
    Rotate the vault encryption key.

    Re-encrypts all vault-stored values with the new key.
    This is an admin-level operation.
    """
    vault = get_vault()

    # Get current config from engine to find encrypted values
    try:
        config = await _send_engine_command("get_config", {})
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to retrieve config from engine: {exc}",
        ) from exc

    if not isinstance(config, dict):
        raise HTTPException(
            status_code=500,
            detail="Unexpected config format from engine",
        )

    # Collect all currently-encrypted vault values
    encrypted_values = {}
    for key in vault.VAULT_KEYS:
        val = _deep_get(config, key)
        if val and isinstance(val, str) and vault.is_encrypted(val):
            encrypted_values[key] = val

    if not encrypted_values:
        return RotateKeyResponse(
            status="ok",
            re_encrypted_count=0,
            message="No encrypted values found to rotate",
        )

    # Rotate key and re-encrypt
    try:
        re_encrypted, _new_key = vault.rotate_key(encrypted_values)
    except VaultDecryptionError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Key rotation failed: {exc}",
        ) from exc

    # Write re-encrypted values back to engine config
    updates = {}
    for key, new_val in re_encrypted.items():
        updates[key] = new_val

    if updates:
        await _send_engine_command("update_config", {"updates": updates})

    logger.info("Vault key rotated, %d values re-encrypted", len(re_encrypted))
    return RotateKeyResponse(
        status="ok",
        re_encrypted_count=len(re_encrypted),
        message=f"Key rotated. {len(re_encrypted)} values re-encrypted.",
    )


def _deep_get(d: dict, dotted_key: str, default=None):
    """Get a value from a nested dict using dot notation."""
    keys = dotted_key.split(".")
    current = d
    for k in keys:
        if isinstance(current, dict):
            current = current.get(k, default)
        else:
            return default
    return current
