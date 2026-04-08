"""
Phase 8: Idempotency Store

Prevents duplicate order submissions by tracking client_order_ids.
Persists to JSON for crash recovery — if we crash after generating
an ID but before confirming, we know on restart that this ID was
already attempted.

Design:
- Deterministic ID generation (same request → same ID)
- Persistent store (survives restarts)
- TTL-based cleanup (old entries expire)
- Thread-safe via RLock

No Qt imports. No execution imports. Pure Python + JSON.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Default TTL: 24 hours (orders older than this are considered expired)
DEFAULT_TTL_MS = 24 * 60 * 60 * 1000

# Maximum entries before forced cleanup
MAX_ENTRIES = 10_000


@dataclass
class IdempotencyEntry:
    """Record of a client_order_id that was generated/submitted."""
    client_order_id: str
    request_id: str
    symbol: str
    side: str
    state: str                  # "generated", "submitted", "confirmed", "failed"
    created_at_ms: int
    updated_at_ms: int
    exchange_order_id: str = ""  # Set when exchange acknowledges
    metadata: str = ""

    def to_dict(self) -> dict:
        return {
            "client_order_id": self.client_order_id,
            "request_id": self.request_id,
            "symbol": self.symbol,
            "side": self.side,
            "state": self.state,
            "created_at_ms": self.created_at_ms,
            "updated_at_ms": self.updated_at_ms,
            "exchange_order_id": self.exchange_order_id,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: dict) -> IdempotencyEntry:
        return cls(
            client_order_id=d["client_order_id"],
            request_id=d["request_id"],
            symbol=d["symbol"],
            side=d["side"],
            state=d["state"],
            created_at_ms=d["created_at_ms"],
            updated_at_ms=d["updated_at_ms"],
            exchange_order_id=d.get("exchange_order_id", ""),
            metadata=d.get("metadata", ""),
        )


class IdempotencyStore:
    """
    Persistent idempotency store for order deduplication.

    Thread-safe. Persists to JSON file. TTL-based cleanup.

    Usage:
        store = IdempotencyStore(Path("data/idempotency.json"))
        store.load()

        # Check before submitting
        if store.exists(client_order_id):
            entry = store.get(client_order_id)
            # Already attempted — don't resubmit, reconcile instead
        else:
            store.register(client_order_id, request_id, symbol, side)
            # Now submit to exchange

        # After exchange confirms
        store.mark_confirmed(client_order_id, exchange_order_id)

        # After order completes or fails
        store.mark_completed(client_order_id)
    """

    def __init__(
        self,
        store_path: Path,
        ttl_ms: int = DEFAULT_TTL_MS,
        now_ms_fn=None,
    ):
        self._store_path = store_path
        self._ttl_ms = ttl_ms
        self._now_ms_fn = now_ms_fn or (lambda: int(time.time() * 1000))
        self._entries: Dict[str, IdempotencyEntry] = {}
        self._lock = threading.RLock()
        self._dirty = False

    # ── Persistence ───────────────────────────────────────────

    def load(self) -> int:
        """
        Load entries from disk. Returns count of entries loaded.

        Idempotent — safe to call multiple times.
        """
        with self._lock:
            if not self._store_path.exists():
                logger.info(f"IdempotencyStore: no file at {self._store_path}, starting empty")
                return 0

            try:
                raw = self._store_path.read_text(encoding="utf-8")
                data = json.loads(raw)
                entries = data.get("entries", [])
                self._entries.clear()
                for entry_dict in entries:
                    entry = IdempotencyEntry.from_dict(entry_dict)
                    self._entries[entry.client_order_id] = entry
                logger.info(f"IdempotencyStore: loaded {len(self._entries)} entries")
                return len(self._entries)
            except Exception as e:
                logger.error(f"IdempotencyStore: failed to load from {self._store_path}: {e}")
                return 0

    def save(self) -> bool:
        """
        Persist entries to disk. Returns True on success.

        Only writes if dirty flag is set.
        """
        with self._lock:
            if not self._dirty:
                return True

            try:
                self._store_path.parent.mkdir(parents=True, exist_ok=True)
                data = {
                    "version": 1,
                    "saved_at_ms": self._now_ms_fn(),
                    "entry_count": len(self._entries),
                    "entries": [e.to_dict() for e in self._entries.values()],
                }
                tmp_path = self._store_path.with_suffix(".tmp")
                tmp_path.write_text(
                    json.dumps(data, indent=2), encoding="utf-8"
                )
                tmp_path.replace(self._store_path)
                self._dirty = False
                return True
            except Exception as e:
                logger.error(f"IdempotencyStore: failed to save: {e}")
                return False

    # ── Core Operations ───────────────────────────────────────

    def exists(self, client_order_id: str) -> bool:
        """Check if a client_order_id has been registered."""
        with self._lock:
            return client_order_id in self._entries

    def get(self, client_order_id: str) -> Optional[IdempotencyEntry]:
        """Get entry by client_order_id, or None if not found."""
        with self._lock:
            return self._entries.get(client_order_id)

    def register(
        self,
        client_order_id: str,
        request_id: str,
        symbol: str,
        side: str,
    ) -> IdempotencyEntry:
        """
        Register a new client_order_id before submission.

        If already exists, returns the existing entry (idempotent).

        Args:
            client_order_id: Deterministic order ID.
            request_id: ExecutionRequest.request_id for traceability.
            symbol: Trading pair.
            side: "buy" or "sell".

        Returns:
            IdempotencyEntry (new or existing).
        """
        with self._lock:
            if client_order_id in self._entries:
                logger.warning(
                    f"IdempotencyStore: duplicate registration for {client_order_id}, "
                    f"returning existing entry (state={self._entries[client_order_id].state})"
                )
                return self._entries[client_order_id]

            now = self._now_ms_fn()
            entry = IdempotencyEntry(
                client_order_id=client_order_id,
                request_id=request_id,
                symbol=symbol,
                side=side,
                state="generated",
                created_at_ms=now,
                updated_at_ms=now,
            )
            self._entries[client_order_id] = entry
            self._dirty = True
            self.save()  # Persist immediately — crash safety

            logger.debug(f"IdempotencyStore: registered {client_order_id} for {symbol} {side}")
            return entry

    def mark_submitted(self, client_order_id: str) -> None:
        """Mark entry as submitted to exchange."""
        with self._lock:
            entry = self._entries.get(client_order_id)
            if entry:
                entry.state = "submitted"
                entry.updated_at_ms = self._now_ms_fn()
                self._dirty = True
                self.save()

    def mark_confirmed(
        self, client_order_id: str, exchange_order_id: str
    ) -> None:
        """Mark entry as confirmed by exchange with exchange_order_id."""
        with self._lock:
            entry = self._entries.get(client_order_id)
            if entry:
                entry.state = "confirmed"
                entry.exchange_order_id = exchange_order_id
                entry.updated_at_ms = self._now_ms_fn()
                self._dirty = True
                self.save()

    def mark_completed(self, client_order_id: str) -> None:
        """Mark entry as completed (order reached terminal state)."""
        with self._lock:
            entry = self._entries.get(client_order_id)
            if entry:
                entry.state = "completed"
                entry.updated_at_ms = self._now_ms_fn()
                self._dirty = True
                # Don't persist immediately for completed — batch is fine

    def mark_failed(self, client_order_id: str, reason: str = "") -> None:
        """Mark entry as failed (submission failed, won't retry)."""
        with self._lock:
            entry = self._entries.get(client_order_id)
            if entry:
                entry.state = "failed"
                entry.metadata = reason
                entry.updated_at_ms = self._now_ms_fn()
                self._dirty = True
                self.save()

    # ── Queries ───────────────────────────────────────────────

    def get_pending_submissions(self) -> list:
        """
        Get entries in 'submitted' state — these are orders that were
        sent to the exchange but never confirmed. Critical for restart
        recovery (crash-after-submit scenario).
        """
        with self._lock:
            return [
                e for e in self._entries.values()
                if e.state == "submitted"
            ]

    def get_generated_not_submitted(self) -> list:
        """
        Get entries in 'generated' state — these were registered but
        never submitted (crash-before-submit). Safe to discard.
        """
        with self._lock:
            return [
                e for e in self._entries.values()
                if e.state == "generated"
            ]

    # ── Maintenance ───────────────────────────────────────────

    def cleanup_expired(self) -> int:
        """
        Remove entries older than TTL. Returns count of entries removed.

        Only removes entries in terminal states (completed, failed).
        Never removes submitted/confirmed — those need reconciliation.
        """
        with self._lock:
            now = self._now_ms_fn()
            expired_keys = []
            for key, entry in self._entries.items():
                age_ms = now - entry.created_at_ms
                if age_ms > self._ttl_ms and entry.state in ("completed", "failed"):
                    expired_keys.append(key)

            for key in expired_keys:
                del self._entries[key]

            if expired_keys:
                self._dirty = True
                logger.info(f"IdempotencyStore: cleaned up {len(expired_keys)} expired entries")

            return len(expired_keys)

    @property
    def entry_count(self) -> int:
        """Total number of entries in the store."""
        with self._lock:
            return len(self._entries)

    def get_state(self) -> dict:
        """Get store state for diagnostics."""
        with self._lock:
            state_counts: Dict[str, int] = {}
            for entry in self._entries.values():
                state_counts[entry.state] = state_counts.get(entry.state, 0) + 1
            return {
                "total_entries": len(self._entries),
                "by_state": state_counts,
                "store_path": str(self._store_path),
                "ttl_ms": self._ttl_ms,
            }
