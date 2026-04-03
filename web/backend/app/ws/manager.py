# ============================================================
# NEXUS TRADER Web — WebSocket Connection Manager
#
# Manages authenticated WebSocket connections with channel
# subscriptions. Bridges Redis pub/sub events to connected
# clients in real-time.
#
# Phase 2C hardening:
#   - Per-message token expiry check
#   - Idle timeout: 30min → disconnect
#   - Max connections per user: 5
#   - Server heartbeat: ping every 30s, client must pong within 10s
# ============================================================
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from typing import Optional

import redis.asyncio as aioredis
from fastapi import WebSocket, WebSocketDisconnect

from app.auth.jwt import decode_access_token
from app.config import get_settings

logger = logging.getLogger(__name__)

# Channel definitions matching the design doc
CHANNELS = {
    "ticker",           # Real-time price updates
    "positions",        # Open position changes
    "trades",           # Trade open/close events
    "scanner",          # Scan cycle results
    "signals",          # Signal generation events
    "engine",           # Engine state changes
    "alerts",           # System alerts and notifications
    "logs",             # Streaming log output
    # Phase 2A additions
    "dashboard",        # Dashboard heartbeat + trade/position changes
    "crash_defense",    # Crash defense tier changes
    "risk",             # Risk status on position open/close
}

# Limits
MAX_CONNECTIONS_PER_USER = 5
IDLE_TIMEOUT_SECONDS = 30 * 60  # 30 minutes
HEARTBEAT_INTERVAL_SECONDS = 30
HEARTBEAT_TIMEOUT_SECONDS = 10

# Phase 6C hardening limits
MESSAGE_RATE_LIMIT = int(os.getenv("NEXUS_WS_RATE_LIMIT", "20"))  # msgs/sec
MESSAGE_SIZE_LIMIT = int(os.getenv("NEXUS_WS_SIZE_LIMIT", str(64 * 1024)))  # 64 KB


class _ConnState:
    """Per-connection state tracking with Phase 6C rate limiting."""

    __slots__ = (
        "ws", "user_sub", "email", "token",
        "last_active", "last_pong",
        "_msg_timestamps",  # rolling window for rate limiting
    )

    def __init__(self, ws: WebSocket, user_sub: str, email: str, token: str):
        self.ws = ws
        self.user_sub = user_sub
        self.email = email
        self.token = token
        self.last_active = time.time()
        self.last_pong = time.time()
        self._msg_timestamps: list[float] = []

    def touch(self):
        self.last_active = time.time()

    def check_rate_limit(self) -> bool:
        """
        Returns True if within rate limit, False if exceeded.
        Uses a 1-second sliding window.
        """
        now = time.time()
        cutoff = now - 1.0
        # Prune old entries
        self._msg_timestamps = [t for t in self._msg_timestamps if t > cutoff]
        if len(self._msg_timestamps) >= MESSAGE_RATE_LIMIT:
            return False
        self._msg_timestamps.append(now)
        return True


class ConnectionManager:
    """
    Manages WebSocket connections and channel subscriptions.
    Each connection is authenticated via JWT token sent on connect.

    Hardening features:
      - Per-message token expiry validation
      - Idle timeout (30 min)
      - Max 5 connections per user
      - Server-initiated heartbeat (30s ping, 10s pong timeout)
    """

    def __init__(self):
        self._connections: dict[str, _ConnState] = {}  # conn_id -> state
        self._subscriptions: dict[str, set[str]] = {}  # channel -> set of conn_ids
        self._user_counts: dict[str, int] = {}  # user_sub -> active conn count
        self._redis_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    def get_user_connection_count(self, user_sub: str) -> int:
        return self._user_counts.get(user_sub, 0)

    async def connect(self, websocket: WebSocket, token: str) -> Optional[str]:
        """
        Authenticate and register a WebSocket connection.
        Returns connection ID on success, None on auth failure.

        Enforces max connections per user.
        """
        payload = decode_access_token(token)
        if payload is None:
            await websocket.close(code=4001, reason="Invalid token")
            return None

        user_sub = payload["sub"]
        email = payload.get("email", "unknown")

        # Enforce max connections per user
        current = self._user_counts.get(user_sub, 0)
        if current >= MAX_CONNECTIONS_PER_USER:
            await websocket.close(
                code=4002,
                reason=f"Max {MAX_CONNECTIONS_PER_USER} connections per user exceeded",
            )
            logger.warning(
                "WS connection rejected: user %s has %d/%d connections",
                email,
                current,
                MAX_CONNECTIONS_PER_USER,
            )
            return None

        await websocket.accept()
        # Cryptographic connection ID (Phase 6C) — not guessable
        conn_id = f"ws_{secrets.token_hex(16)}"
        self._connections[conn_id] = _ConnState(
            ws=websocket, user_sub=user_sub, email=email, token=token,
        )
        self._user_counts[user_sub] = current + 1
        logger.info(
            "WebSocket connected: %s (user=%s, connections=%d)",
            conn_id, email, current + 1,
        )
        return conn_id

    def disconnect(self, conn_id: str):
        """Remove a connection and all its subscriptions."""
        state = self._connections.pop(conn_id, None)
        if state:
            count = self._user_counts.get(state.user_sub, 1)
            if count <= 1:
                self._user_counts.pop(state.user_sub, None)
            else:
                self._user_counts[state.user_sub] = count - 1
        for channel_subs in self._subscriptions.values():
            channel_subs.discard(conn_id)
        logger.info("WebSocket disconnected: %s", conn_id)

    def validate_token(self, conn_id: str) -> bool:
        """
        Re-validate the token for a connection. Returns True if still valid.
        Called on every message to catch expired tokens mid-session.
        """
        state = self._connections.get(conn_id)
        if state is None:
            return False
        payload = decode_access_token(state.token)
        return payload is not None

    def touch(self, conn_id: str):
        """Update last-active timestamp for idle timeout tracking."""
        state = self._connections.get(conn_id)
        if state:
            state.touch()

    def record_pong(self, conn_id: str):
        """Record that client responded to server heartbeat ping."""
        state = self._connections.get(conn_id)
        if state:
            state.last_pong = time.time()
            state.touch()

    def subscribe(self, conn_id: str, channel: str) -> bool:
        """Subscribe a connection to a channel. Returns False if channel invalid."""
        if channel not in CHANNELS:
            return False
        if channel not in self._subscriptions:
            self._subscriptions[channel] = set()
        self._subscriptions[channel].add(conn_id)
        return True

    def unsubscribe(self, conn_id: str, channel: str):
        """Unsubscribe a connection from a channel."""
        if channel in self._subscriptions:
            self._subscriptions[channel].discard(conn_id)

    async def broadcast(self, channel: str, data: dict):
        """Send a message to all subscribers of a channel."""
        conn_ids = self._subscriptions.get(channel, set())
        if not conn_ids:
            return

        message = json.dumps({"channel": channel, "data": data})
        dead = []
        for conn_id in conn_ids:
            state = self._connections.get(conn_id)
            if state is None:
                dead.append(conn_id)
                continue
            try:
                await state.ws.send_text(message)
            except Exception as e:
                logger.warning(
                    "WS broadcast send failed for %s: %s", conn_id, e,
                )
                dead.append(conn_id)

        for conn_id in dead:
            self.disconnect(conn_id)

    async def send_personal(self, conn_id: str, data: dict):
        """Send a message to a specific connection."""
        state = self._connections.get(conn_id)
        if state is not None:
            try:
                await state.ws.send_text(json.dumps(data))
            except Exception as e:
                logger.warning(
                    "WS send_personal failed for %s: %s", conn_id, e,
                )
                self.disconnect(conn_id)

    async def close_connection(self, conn_id: str, code: int, reason: str):
        """Gracefully close a WebSocket connection."""
        state = self._connections.get(conn_id)
        if state is not None:
            try:
                await state.ws.close(code=code, reason=reason)
            except Exception as e:
                logger.debug(
                    "WS close_connection expected error for %s: %s",
                    conn_id, e,
                )
            self.disconnect(conn_id)

    # ── Heartbeat & Idle Timeout ───────────────────────────

    async def start_heartbeat(self):
        """Start background heartbeat task."""
        if self._heartbeat_task is not None:
            return
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def stop_heartbeat(self):
        """Stop heartbeat task."""
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

    async def _heartbeat_loop(self):
        """Send pings every HEARTBEAT_INTERVAL_SECONDS and check for timeouts."""
        while True:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
                now = time.time()
                dead = []

                for conn_id, state in list(self._connections.items()):
                    # Check idle timeout
                    if (now - state.last_active) > IDLE_TIMEOUT_SECONDS:
                        logger.info(
                            "WS idle timeout: %s (user=%s, idle=%.0fs)",
                            conn_id, state.email, now - state.last_active,
                        )
                        dead.append((conn_id, 4003, "Idle timeout"))
                        continue

                    # Check heartbeat timeout (no pong after 2 intervals)
                    if (now - state.last_pong) > (HEARTBEAT_INTERVAL_SECONDS + HEARTBEAT_TIMEOUT_SECONDS) * 2:
                        logger.info(
                            "WS heartbeat timeout: %s (user=%s)",
                            conn_id, state.email,
                        )
                        dead.append((conn_id, 4004, "Heartbeat timeout"))
                        continue

                    # Send server ping
                    try:
                        await state.ws.send_text(json.dumps({"type": "ping"}))
                    except Exception as e:
                        logger.warning(
                            "WS heartbeat ping failed for %s: %s", conn_id, e,
                        )
                        dead.append((conn_id, 1000, "Connection lost"))

                for conn_id, code, reason in dead:
                    await self.close_connection(conn_id, code, reason)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Heartbeat loop error: %s", e, exc_info=True)

    # ── Redis Bridge ───────────────────────────────────────

    async def start_redis_listener(self):
        """Start background task that bridges Redis pub/sub to WebSocket clients."""
        if self._redis_task is not None:
            return
        self._redis_task = asyncio.create_task(self._redis_bridge_loop())
        await self.start_heartbeat()

    async def stop_redis_listener(self):
        """Stop the Redis bridge task and heartbeat."""
        await self.stop_heartbeat()
        if self._redis_task is not None:
            self._redis_task.cancel()
            try:
                await self._redis_task
            except asyncio.CancelledError:
                pass
            self._redis_task = None

    async def _redis_bridge_loop(self):
        """
        Subscribe to Redis nexus:events:* channels and forward to
        WebSocket clients based on channel mapping.
        """
        settings = get_settings()
        while True:
            try:
                r = aioredis.from_url(settings.redis_url, decode_responses=True)
                pubsub = r.pubsub()
                await pubsub.psubscribe("nexus:events:*")

                async for message in pubsub.listen():
                    if message["type"] != "pmessage":
                        continue

                    try:
                        # Channel format: nexus:events:{channel_name}
                        redis_channel = message["channel"]
                        ws_channel = redis_channel.split(":", 2)[-1] if ":" in redis_channel else redis_channel
                        data = json.loads(message["data"])
                        await self.broadcast(ws_channel, data)
                    except (json.JSONDecodeError, KeyError) as e:
                        logger.warning("Malformed Redis event: %s", e)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Redis bridge error, reconnecting in 5s: %s", e)
                await asyncio.sleep(5)


# Singleton instance
ws_manager = ConnectionManager()
