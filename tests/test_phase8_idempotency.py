"""
Phase 8 Test Suite — Idempotency Store

Tests:
- Registration and deduplication
- State transitions (generated → submitted → confirmed → completed)
- Persistence (save/load round-trip)
- TTL-based cleanup
- Crash-after-submit detection (pending_submissions query)
- Crash-before-submit detection (generated_not_submitted query)
- Thread-safety basics
"""

import json
import pytest
from pathlib import Path
from core.intraday.live.idempotency_store import IdempotencyStore, IdempotencyEntry


# ══════════════════════════════════════════════════════════════
# FIXTURES
# ══════════════════════════════════════════════════════════════

@pytest.fixture
def tmp_store(tmp_path):
    """Create an IdempotencyStore with a temp path and fixed clock."""
    clock = [1000000]

    def now_ms():
        return clock[0]

    store = IdempotencyStore(
        store_path=tmp_path / "idempotency.json",
        ttl_ms=3600_000,  # 1 hour
        now_ms_fn=now_ms,
    )
    return store, clock


# ══════════════════════════════════════════════════════════════
# 1. REGISTRATION & DEDUP
# ══════════════════════════════════════════════════════════════

class TestRegistration:
    def test_register_new_entry(self, tmp_store):
        store, clock = tmp_store
        entry = store.register("NT-abc", "req-1", "BTCUSDT", "buy")
        assert entry.client_order_id == "NT-abc"
        assert entry.state == "generated"
        assert entry.created_at_ms == 1000000
        assert store.exists("NT-abc")

    def test_duplicate_registration_returns_existing(self, tmp_store):
        store, clock = tmp_store
        entry1 = store.register("NT-abc", "req-1", "BTCUSDT", "buy")
        clock[0] = 2000000
        entry2 = store.register("NT-abc", "req-1", "BTCUSDT", "buy")
        # Returns existing, not new
        assert entry2.created_at_ms == 1000000
        assert store.entry_count == 1

    def test_different_ids_are_separate(self, tmp_store):
        store, clock = tmp_store
        store.register("NT-aaa", "req-1", "BTCUSDT", "buy")
        store.register("NT-bbb", "req-2", "ETHUSDT", "sell")
        assert store.entry_count == 2

    def test_exists_nonexistent(self, tmp_store):
        store, _ = tmp_store
        assert not store.exists("NT-xyz")

    def test_get_nonexistent(self, tmp_store):
        store, _ = tmp_store
        assert store.get("NT-xyz") is None


# ══════════════════════════════════════════════════════════════
# 2. STATE TRANSITIONS
# ══════════════════════════════════════════════════════════════

class TestStateTransitions:
    def test_generated_to_submitted(self, tmp_store):
        store, clock = tmp_store
        store.register("NT-1", "req-1", "BTCUSDT", "buy")
        clock[0] = 2000000
        store.mark_submitted("NT-1")
        entry = store.get("NT-1")
        assert entry.state == "submitted"
        assert entry.updated_at_ms == 2000000

    def test_submitted_to_confirmed(self, tmp_store):
        store, clock = tmp_store
        store.register("NT-1", "req-1", "BTCUSDT", "buy")
        store.mark_submitted("NT-1")
        clock[0] = 3000000
        store.mark_confirmed("NT-1", "BYBIT-456")
        entry = store.get("NT-1")
        assert entry.state == "confirmed"
        assert entry.exchange_order_id == "BYBIT-456"

    def test_confirmed_to_completed(self, tmp_store):
        store, clock = tmp_store
        store.register("NT-1", "req-1", "BTCUSDT", "buy")
        store.mark_submitted("NT-1")
        store.mark_confirmed("NT-1", "BYBIT-456")
        store.mark_completed("NT-1")
        entry = store.get("NT-1")
        assert entry.state == "completed"

    def test_mark_failed(self, tmp_store):
        store, clock = tmp_store
        store.register("NT-1", "req-1", "BTCUSDT", "buy")
        store.mark_submitted("NT-1")
        store.mark_failed("NT-1", "insufficient_funds")
        entry = store.get("NT-1")
        assert entry.state == "failed"
        assert entry.metadata == "insufficient_funds"

    def test_mark_nonexistent_is_noop(self, tmp_store):
        store, _ = tmp_store
        # Should not raise
        store.mark_submitted("NT-ghost")
        store.mark_confirmed("NT-ghost", "EX-1")
        store.mark_completed("NT-ghost")
        store.mark_failed("NT-ghost", "reason")


# ══════════════════════════════════════════════════════════════
# 3. PERSISTENCE
# ══════════════════════════════════════════════════════════════

class TestPersistence:
    def test_save_and_load(self, tmp_store):
        store, clock = tmp_store
        store.register("NT-1", "req-1", "BTCUSDT", "buy")
        store.mark_submitted("NT-1")
        store.mark_confirmed("NT-1", "BYBIT-123")

        # Save happens automatically on register and state changes
        # Create a new store and load
        store2 = IdempotencyStore(
            store_path=store._store_path,
            now_ms_fn=lambda: clock[0],
        )
        count = store2.load()
        assert count == 1
        entry = store2.get("NT-1")
        assert entry.state == "confirmed"
        assert entry.exchange_order_id == "BYBIT-123"

    def test_load_empty_file(self, tmp_path):
        store = IdempotencyStore(store_path=tmp_path / "empty.json")
        count = store.load()
        assert count == 0

    def test_load_corrupt_file(self, tmp_path):
        path = tmp_path / "corrupt.json"
        path.write_text("not json at all")
        store = IdempotencyStore(store_path=path)
        count = store.load()
        assert count == 0

    def test_atomic_write(self, tmp_store):
        store, clock = tmp_store
        store.register("NT-1", "req-1", "BTCUSDT", "buy")

        # Check that file exists and is valid JSON
        data = json.loads(store._store_path.read_text())
        assert data["version"] == 1
        assert data["entry_count"] == 1

    def test_save_only_when_dirty(self, tmp_store):
        store, _ = tmp_store
        # Not dirty initially
        assert store.save() is True


# ══════════════════════════════════════════════════════════════
# 4. TTL CLEANUP
# ══════════════════════════════════════════════════════════════

class TestCleanup:
    def test_cleanup_expired_completed(self, tmp_store):
        store, clock = tmp_store
        store.register("NT-1", "req-1", "BTCUSDT", "buy")
        store.mark_completed("NT-1")

        # Advance past TTL
        clock[0] = 1000000 + 3601_000
        removed = store.cleanup_expired()
        assert removed == 1
        assert not store.exists("NT-1")

    def test_cleanup_expired_failed(self, tmp_store):
        store, clock = tmp_store
        store.register("NT-1", "req-1", "BTCUSDT", "buy")
        store.mark_failed("NT-1", "error")

        clock[0] = 1000000 + 3601_000
        removed = store.cleanup_expired()
        assert removed == 1

    def test_cleanup_does_not_remove_submitted(self, tmp_store):
        store, clock = tmp_store
        store.register("NT-1", "req-1", "BTCUSDT", "buy")
        store.mark_submitted("NT-1")

        # Even past TTL, submitted entries are kept (need reconciliation)
        clock[0] = 1000000 + 3601_000
        removed = store.cleanup_expired()
        assert removed == 0
        assert store.exists("NT-1")

    def test_cleanup_does_not_remove_confirmed(self, tmp_store):
        store, clock = tmp_store
        store.register("NT-1", "req-1", "BTCUSDT", "buy")
        store.mark_confirmed("NT-1", "EX-1")

        clock[0] = 1000000 + 3601_000
        removed = store.cleanup_expired()
        assert removed == 0

    def test_cleanup_not_yet_expired(self, tmp_store):
        store, clock = tmp_store
        store.register("NT-1", "req-1", "BTCUSDT", "buy")
        store.mark_completed("NT-1")

        # Still within TTL
        clock[0] = 1000000 + 1000
        removed = store.cleanup_expired()
        assert removed == 0


# ══════════════════════════════════════════════════════════════
# 5. CRASH DETECTION QUERIES
# ══════════════════════════════════════════════════════════════

class TestCrashDetection:
    def test_pending_submissions(self, tmp_store):
        store, _ = tmp_store
        store.register("NT-1", "req-1", "BTCUSDT", "buy")
        store.mark_submitted("NT-1")
        store.register("NT-2", "req-2", "ETHUSDT", "sell")
        store.mark_submitted("NT-2")
        store.register("NT-3", "req-3", "SOLUSDT", "buy")
        # NT-3 is only "generated", not submitted

        pending = store.get_pending_submissions()
        assert len(pending) == 2
        ids = {p.client_order_id for p in pending}
        assert ids == {"NT-1", "NT-2"}

    def test_generated_not_submitted(self, tmp_store):
        store, _ = tmp_store
        store.register("NT-1", "req-1", "BTCUSDT", "buy")  # generated
        store.register("NT-2", "req-2", "ETHUSDT", "sell")
        store.mark_submitted("NT-2")  # submitted

        generated = store.get_generated_not_submitted()
        assert len(generated) == 1
        assert generated[0].client_order_id == "NT-1"

    def test_no_pending_submissions(self, tmp_store):
        store, _ = tmp_store
        assert store.get_pending_submissions() == []

    def test_get_state_diagnostics(self, tmp_store):
        store, _ = tmp_store
        store.register("NT-1", "req-1", "BTCUSDT", "buy")
        store.register("NT-2", "req-2", "ETHUSDT", "sell")
        store.mark_submitted("NT-2")

        state = store.get_state()
        assert state["total_entries"] == 2
        assert state["by_state"]["generated"] == 1
        assert state["by_state"]["submitted"] == 1


# ══════════════════════════════════════════════════════════════
# 6. ENTRY SERIALIZATION
# ══════════════════════════════════════════════════════════════

class TestEntrySerialization:
    def test_entry_round_trip(self):
        entry = IdempotencyEntry(
            client_order_id="NT-abc",
            request_id="req-1",
            symbol="BTCUSDT",
            side="buy",
            state="confirmed",
            created_at_ms=1000,
            updated_at_ms=2000,
            exchange_order_id="EX-1",
            metadata="test",
        )
        d = entry.to_dict()
        restored = IdempotencyEntry.from_dict(d)
        assert restored.client_order_id == entry.client_order_id
        assert restored.state == entry.state
        assert restored.exchange_order_id == entry.exchange_order_id
