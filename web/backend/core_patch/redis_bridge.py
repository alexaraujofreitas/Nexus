# ============================================================
# NEXUS TRADER — Redis Event Bridge
#
# Bridges the local EventBus to Redis pub/sub for cross-process
# event distribution.  Handles:
#   - Publishing local events to Redis channels
#   - Subscribing to Redis channels and dispatching to local bus
#   - Serialisation / deserialisation of Event objects
#   - Connection health monitoring with reconnect
# ============================================================
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from core_patch.event_bus import Event, EventBus

logger = logging.getLogger(__name__)

# Default Redis channel prefix
CHANNEL_PREFIX = "nexus"


def _safe_json(obj: Any) -> Any:
    """Make arbitrary data JSON-serialisable."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _safe_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_safe_json(v) for v in obj]
    # Fallback: str()
    return str(obj)


class RedisBridge:
    """
    Bridges a local EventBus instance to Redis pub/sub.

    Usage:
        bridge = RedisBridge(redis_url="redis://localhost:6379/0", service_name="engine")
        bridge.start(bus)
        # Events published to bus are now also published to Redis.
        # Events from other services on Redis are dispatched to local bus.
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379/0",
        service_name: str = "api",
        publish_channel: Optional[str] = None,
        subscribe_channels: Optional[list[str]] = None,
    ):
        self._redis_url = redis_url
        self._service_name = service_name
        self._publish_channel = publish_channel or f"{CHANNEL_PREFIX}:{service_name}:events"
        self._subscribe_channels = subscribe_channels or []
        self._bus: Optional["EventBus"] = None
        self._redis = None  # redis.Redis instance (lazy)
        self._pubsub = None
        self._listener_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._connected = False

    def start(self, bus: "EventBus") -> None:
        """Connect to Redis and start listening for events."""
        self._bus = bus
        bus.attach_redis_bridge(self)

        try:
            import redis
            self._redis = redis.Redis.from_url(
                self._redis_url,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_keepalive=True,
                retry_on_timeout=True,
            )
            # Verify connectivity
            self._redis.ping()
            self._connected = True
            logger.info(
                "Redis bridge connected: publish=%s, subscribe=%s",
                self._publish_channel,
                self._subscribe_channels,
            )
        except Exception as e:
            logger.error("Redis bridge connection failed: %s", e)
            self._connected = False
            return

        # Start listener thread for subscribed channels
        if self._subscribe_channels:
            self._pubsub = self._redis.pubsub(ignore_subscribe_messages=True)
            self._pubsub.subscribe(*self._subscribe_channels)
            self._listener_thread = threading.Thread(
                target=self._listen_loop,
                name=f"redis-bridge-{self._service_name}",
                daemon=True,
            )
            self._listener_thread.start()

    def stop(self) -> None:
        """Stop the listener and disconnect."""
        self._stop_event.set()
        if self._pubsub:
            try:
                self._pubsub.unsubscribe()
                self._pubsub.close()
            except Exception:
                pass
        if self._listener_thread and self._listener_thread.is_alive():
            self._listener_thread.join(timeout=3)
        if self._redis:
            try:
                self._redis.close()
            except Exception:
                pass
        self._connected = False
        logger.info("Redis bridge stopped")

    @property
    def is_connected(self) -> bool:
        return self._connected

    def publish_event(self, event: "Event") -> None:
        """Publish an event to the Redis channel."""
        if not self._connected or self._redis is None:
            return
        try:
            payload = json.dumps({
                "topic": event.topic,
                "data": _safe_json(event.data),
                "source": event.source,
                "ts": event.timestamp.isoformat(),
                "origin_service": self._service_name,
            })
            self._redis.publish(self._publish_channel, payload)
        except Exception as e:
            logger.warning("Redis publish failed for topic '%s': %s", event.topic, e)

    def _listen_loop(self) -> None:
        """Background thread: listen for Redis messages and dispatch to local bus."""
        logger.info("Redis listener started for channels: %s", self._subscribe_channels)
        while not self._stop_event.is_set():
            try:
                message = self._pubsub.get_message(timeout=1.0)
                if message is None:
                    continue
                if message["type"] != "message":
                    continue
                self._handle_message(message["data"])
            except Exception as e:
                if self._stop_event.is_set():
                    break
                logger.error("Redis listener error: %s", e)
                time.sleep(1)  # Brief pause before retry

        logger.info("Redis listener stopped")

    def _handle_message(self, raw: str) -> None:
        """Parse a Redis message and dispatch to local bus."""
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Invalid JSON from Redis: %s", raw[:200])
            return

        # Prevent echo: don't re-dispatch events from our own service
        if payload.get("origin_service") == self._service_name:
            return

        topic = payload.get("topic")
        if not topic:
            return

        # Dispatch to local bus (without re-publishing to Redis)
        if self._bus is not None:
            from core_patch.event_bus import Event
            event = Event(
                topic=topic,
                data=payload.get("data"),
                source=payload.get("source", "redis"),
            )
            # Inject directly into subscribers, bypassing publish()
            # to avoid re-publishing to Redis (infinite loop prevention)
            with self._bus._lock:
                callbacks = list(self._bus._subscribers.get(topic, []))
                callbacks += list(self._bus._subscribers.get("*", []))

            for cb in callbacks:
                try:
                    cb(event)
                except Exception as e:
                    logger.error(
                        "Redis→local dispatch error for '%s': %s", topic, e, exc_info=True
                    )

    # ── Command Queue (Request/Reply) ──────────────────────
    def send_command(
        self,
        cmd: str,
        params: dict,
        idempotency_key: str,
        timeout_s: float = 5.0,
    ) -> dict:
        """
        Send a command to the engine via Redis request/reply queue.
        Returns the engine's response dict.
        Raises TimeoutError if no reply within timeout_s.
        """
        if not self._connected or self._redis is None:
            raise ConnectionError("Redis bridge not connected")

        import uuid
        request_id = str(uuid.uuid4())
        reply_key = f"{CHANNEL_PREFIX}:api:replies:{request_id}"

        command_payload = json.dumps({
            "id": request_id,
            "cmd": cmd,
            "params": _safe_json(params),
            "idempotency_key": idempotency_key,
            "reply_to": reply_key,
            "ts": time.time(),
            "timeout_ms": int(timeout_s * 1000),
        })

        # Push command to engine's command queue
        self._redis.rpush(f"{CHANNEL_PREFIX}:engine:commands", command_payload)

        # Wait for reply with BLPOP
        result = self._redis.blpop(reply_key, timeout=timeout_s)
        if result is None:
            raise TimeoutError(f"Engine command '{cmd}' timed out after {timeout_s}s")

        # result is (key, value) tuple
        try:
            return json.loads(result[1])
        except (json.JSONDecodeError, IndexError) as e:
            raise ValueError(f"Invalid reply from engine: {e}")

    # ── State Hash Helpers ────────────────────────────────
    def set_state(self, key: str, data: dict) -> None:
        """Write a state hash to Redis (e.g., nexus:state:engine)."""
        if not self._connected or self._redis is None:
            return
        try:
            self._redis.set(
                f"{CHANNEL_PREFIX}:state:{key}",
                json.dumps(_safe_json(data)),
            )
        except Exception as e:
            logger.warning("Redis set_state failed for '%s': %s", key, e)

    def get_state(self, key: str) -> Optional[dict]:
        """Read a state hash from Redis."""
        if not self._connected or self._redis is None:
            return None
        try:
            raw = self._redis.get(f"{CHANNEL_PREFIX}:state:{key}")
            if raw:
                return json.loads(raw)
        except Exception as e:
            logger.warning("Redis get_state failed for '%s': %s", key, e)
        return None
