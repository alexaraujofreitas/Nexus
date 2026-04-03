# ============================================================
# NEXUS TRADER Web — WebSocket Route
#
# ws:///ws?token=<jwt>
# Client sends JSON: {"action": "subscribe", "channel": "ticker"}
# Server sends JSON: {"channel": "ticker", "data": {...}}
#
# Phase 2C hardening:
#   - Per-message token expiry check
#   - Pong response handling for heartbeat
#   - Token refresh support via re-auth action
# ============================================================
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.ws.manager import ws_manager, MESSAGE_SIZE_LIMIT

logger = logging.getLogger(__name__)
router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = ""):
    """
    WebSocket endpoint with JWT authentication via query param.
    Protocol:
      - Client sends: {"action": "subscribe"|"unsubscribe"|"ping"|"pong"|"reauth", ...}
      - Server sends: {"channel": "<name>", "data": {...}} or {"type": "..."}
    """
    conn_id = await ws_manager.connect(websocket, token)
    if conn_id is None:
        return  # Auth failed, connection closed

    try:
        while True:
            raw = await websocket.receive_text()

            # Phase 6C: Message size limit
            if len(raw) > MESSAGE_SIZE_LIMIT:
                await ws_manager.send_personal(conn_id, {
                    "type": "error",
                    "detail": f"Message exceeds size limit ({MESSAGE_SIZE_LIMIT} bytes)",
                    "code": "MESSAGE_TOO_LARGE",
                })
                continue

            # Phase 6C: Per-client rate limiting
            state = ws_manager._connections.get(conn_id)
            if state and not state.check_rate_limit():
                await ws_manager.send_personal(conn_id, {
                    "type": "error",
                    "detail": "Rate limit exceeded. Slow down.",
                    "code": "RATE_LIMITED",
                })
                continue

            try:
                msg = json.loads(raw)
                action = msg.get("action")

                # Per-message token expiry check
                if action not in ("pong", "ping"):
                    if not ws_manager.validate_token(conn_id):
                        await ws_manager.send_personal(conn_id, {
                            "type": "error",
                            "detail": "Token expired. Please reconnect.",
                            "code": "TOKEN_EXPIRED",
                        })
                        await ws_manager.close_connection(
                            conn_id, 4001, "Token expired",
                        )
                        return

                # Update activity timestamp
                ws_manager.touch(conn_id)

                if action == "subscribe":
                    channel = msg.get("channel", "")
                    ok = ws_manager.subscribe(conn_id, channel)
                    if ok:
                        await ws_manager.send_personal(conn_id, {
                            "type": "subscribed",
                            "channel": channel,
                        })
                    else:
                        await ws_manager.send_personal(conn_id, {
                            "type": "error",
                            "detail": f"Unknown channel: {channel}",
                            "code": "INVALID_CHANNEL",
                        })
                elif action == "unsubscribe":
                    channel = msg.get("channel", "")
                    ws_manager.unsubscribe(conn_id, channel)
                    await ws_manager.send_personal(conn_id, {
                        "type": "unsubscribed",
                        "channel": channel,
                    })
                elif action == "ping":
                    await ws_manager.send_personal(conn_id, {"type": "pong"})
                elif action == "pong":
                    # Client responding to server heartbeat ping
                    ws_manager.record_pong(conn_id)
                elif action == "reauth":
                    # Token refresh: client sends new token to extend session
                    new_token = msg.get("token", "")
                    from app.auth.jwt import decode_access_token
                    payload = decode_access_token(new_token)
                    if payload:
                        state = ws_manager._connections.get(conn_id)
                        if state:
                            state.token = new_token
                        await ws_manager.send_personal(conn_id, {
                            "type": "reauth_ok",
                        })
                    else:
                        await ws_manager.send_personal(conn_id, {
                            "type": "error",
                            "detail": "Invalid reauth token",
                            "code": "INVALID_TOKEN",
                        })
                else:
                    await ws_manager.send_personal(conn_id, {
                        "type": "error",
                        "detail": f"Unknown action: {action}",
                    })
            except json.JSONDecodeError:
                await ws_manager.send_personal(conn_id, {
                    "type": "error",
                    "detail": "Invalid JSON",
                })
    except WebSocketDisconnect:
        ws_manager.disconnect(conn_id)
