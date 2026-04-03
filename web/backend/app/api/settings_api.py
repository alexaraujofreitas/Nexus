# ============================================================
# NEXUS TRADER Web — Settings Router
#
# GET   /settings/       — get runtime config
# PATCH /settings/       — update config values
# ============================================================
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.auth.dependencies import get_current_user
from app.api.engine import _send_engine_command

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/settings",
    tags=["settings"],
    dependencies=[Depends(get_current_user)],
)


class ConfigUpdate(BaseModel):
    updates: dict  # {key: value, ...}


@router.get("/")
async def get_config(section: str | None = None):
    """Get runtime configuration (full or by section)."""
    params = {}
    if section:
        params["section"] = section
    return await _send_engine_command("get_config", params)


@router.patch("/")
async def update_config(body: ConfigUpdate):
    """Update runtime configuration values. Persists to disk."""
    return await _send_engine_command("update_config", {"updates": body.updates})
