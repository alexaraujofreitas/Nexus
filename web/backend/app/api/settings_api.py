# ============================================================
# NEXUS TRADER Web — Settings Router
#
# GET   /settings/                     — get runtime config (vault keys masked)
# PATCH /settings/                     — update config values (vault keys encrypted)
# POST  /settings/notifications/test/{channel} — test notification channel
# POST  /settings/notifications/test-all       — test all configured channels
# GET   /settings/notifications/history        — notification delivery history
# GET   /settings/notifications/stats          — delivery statistics
# PUT   /settings/notifications/preferences    — update notification preferences
# PUT   /settings/notifications/health-check-interval — set health check interval
# ============================================================
from __future__ import annotations

import logging
from typing import Optional, Dict, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.dependencies import get_current_user
from app.api.engine import _send_engine_command
from app.services.vault import get_vault, VaultDecryptionError

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/settings",
    tags=["settings"],
    dependencies=[Depends(get_current_user)],
)


class ConfigUpdate(BaseModel):
    updates: dict  # {key: value, ...}


class NotificationPreferencesUpdate(BaseModel):
    preferences: Dict[str, bool]  # {notification_type: enabled, ...}


class HealthCheckIntervalUpdate(BaseModel):
    hours: int  # Must be one of: 1, 2, 3, 4, 6, 12, 24


VALID_CHANNELS = {"whatsapp", "telegram", "email", "gemini", "sms"}

VALID_HEALTH_CHECK_INTERVALS = {1, 2, 3, 4, 6, 12, 24}

VALID_NOTIFICATION_TYPES = {
    "trade_opened", "trade_closed", "trade_stopped", "trade_rejected",
    "trade_modified", "strategy_signal", "risk_warning", "market_condition",
    "system_error", "emergency_stop", "daily_summary", "health_check",
}


def _mask_vault_keys_in_config(config: dict) -> dict:
    """
    Recursively walk the config dict and mask any vault-key values.

    For flat keys like 'ai.anthropic_api_key' we check if the
    dotted path of the current position matches a vault key.
    Also handles the case where engine returns a nested dict.
    """
    vault = get_vault()

    def _walk(d: dict, prefix: str = "") -> dict:
        result = {}
        for k, v in d.items():
            full_key = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                result[k] = _walk(v, full_key)
            elif isinstance(v, str) and vault.is_vault_key(full_key):
                # Decrypt first if encrypted, then mask for display
                if vault.is_encrypted(v):
                    try:
                        plain = vault.decrypt(v)
                        result[k] = vault.mask(plain)
                    except VaultDecryptionError:
                        result[k] = "****[decrypt error]"
                elif v:
                    # Plaintext value (legacy / not yet encrypted)
                    result[k] = vault.mask(v)
                else:
                    result[k] = ""
            else:
                result[k] = v
        return result

    if isinstance(config, dict):
        return _walk(config)
    return config


def _encrypt_vault_keys_in_updates(updates: dict) -> dict:
    """
    Encrypt any vault-key values in the updates dict before
    sending to the engine for persistence.
    """
    vault = get_vault()
    encrypted = {}

    for key, value in updates.items():
        if vault.is_vault_key(key) and isinstance(value, str) and value:
            # Don't re-encrypt already-encrypted values
            if not vault.is_encrypted(value):
                encrypted[key] = vault.encrypt(value)
            else:
                encrypted[key] = value
        else:
            encrypted[key] = value

    return encrypted


@router.get("/")
async def get_config(section: Optional[str] = None):
    """Get runtime configuration (full or by section). Vault keys are masked."""
    params = {}
    if section:
        params["section"] = section
    config = await _send_engine_command("get_config", params)

    # Mask vault keys in response
    return _mask_vault_keys_in_config(config)


@router.patch("/")
async def update_config(body: ConfigUpdate):
    """Update runtime configuration values. Vault keys are encrypted before persistence."""
    # Encrypt vault keys before sending to engine
    encrypted_updates = _encrypt_vault_keys_in_updates(body.updates)
    return await _send_engine_command("update_config", {"updates": encrypted_updates})


@router.post("/notifications/test/{channel}")
async def test_notification_channel(channel: str):
    """
    Test a notification channel by sending a test message.

    Channels: whatsapp, telegram, email, gemini, sms
    """
    if channel not in VALID_CHANNELS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid channel '{channel}'. Must be one of: {', '.join(sorted(VALID_CHANNELS))}",
        )

    result = await _send_engine_command(
        "notification.test",
        {"channel": channel},
        timeout=15,
    )
    return result


@router.post("/notifications/test-all")
async def test_all_notification_channels():
    """
    Test all configured notification channels at once.
    Returns a dict of {channel: success_bool} for each configured channel.
    """
    result = await _send_engine_command(
        "notification.test_all",
        {},
        timeout=30,
    )
    return result


@router.get("/notifications/history")
async def get_notification_history(limit: int = 50):
    """
    Get recent notification delivery history.
    Returns list of sent notifications with timestamps, channels, and status.
    """
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="Limit must be between 1 and 500")

    result = await _send_engine_command(
        "notification.get_history",
        {"limit": limit},
        timeout=10,
    )
    return result


@router.get("/notifications/stats")
async def get_notification_stats():
    """
    Get notification delivery statistics.
    Returns total_sent, total_failed, total_retried, success_rate.
    """
    result = await _send_engine_command(
        "notification.get_stats",
        {},
        timeout=10,
    )
    return result


@router.put("/notifications/preferences")
async def update_notification_preferences(body: NotificationPreferencesUpdate):
    """
    Update which notification types are enabled/disabled.
    Valid types: trade_opened, trade_closed, trade_stopped, trade_rejected,
    trade_modified, strategy_signal, risk_warning, market_condition,
    system_error, emergency_stop, daily_summary, health_check.
    """
    invalid_types = set(body.preferences.keys()) - VALID_NOTIFICATION_TYPES
    if invalid_types:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid notification types: {', '.join(sorted(invalid_types))}. "
                   f"Valid types: {', '.join(sorted(VALID_NOTIFICATION_TYPES))}",
        )

    result = await _send_engine_command(
        "notification.set_preferences",
        {"preferences": body.preferences},
        timeout=10,
    )
    return result


@router.put("/notifications/health-check-interval")
async def update_health_check_interval(body: HealthCheckIntervalUpdate):
    """
    Set the health check notification interval in hours.
    Valid values: 1, 2, 3, 4, 6, 12, 24.
    """
    if body.hours not in VALID_HEALTH_CHECK_INTERVALS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid interval {body.hours}h. "
                   f"Must be one of: {', '.join(str(h) for h in sorted(VALID_HEALTH_CHECK_INTERVALS))}",
        )

    result = await _send_engine_command(
        "notification.set_health_check_interval",
        {"hours": body.hours},
        timeout=10,
    )
    return result
