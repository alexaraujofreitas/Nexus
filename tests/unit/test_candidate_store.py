"""
Tests for core.scanning.candidate_store — staged candidate lifecycle.

Naming: CS2-xxx (CS2 to avoid collision with existing CS = confluence scorer tests)

Covers:
  S1: Creation and basic properties
  S2: Fingerprint dedup and refresh
  S3: State transitions (confirm, execute, void, expire)
  S4: Capacity limits
  S5: Concurrency safety
  S6: Expiry and cleanup
  S7: Query methods
  S8: Transition log / audit trail
  S9: Edge cases
  S10: Config integration
"""

import threading
import time
from unittest.mock import patch

import pytest

from core.scanning.candidate_store import (
    CandidateState,
    CandidateStore,
    StagedCandidate,
    make_fingerprint,
    get_candidate_store,
)


# ── Fixtures ───────────────────────────────────────────────────────────

def _cd(symbol="BTC/USDT", side="buy", score=0.75,
        models=None, regime="bull_trend", **extra):
    """Build a minimal candidate dict like OrderCandidate.to_dict()."""
    d = {
        "symbol": symbol,
        "side": side,
        "score": score,
        "entry_price": 87000.0,
        "stop_loss_price": 86000.0,
        "take_profit_price": 89000.0,
        "models_fired": models or ["trend"],
        "regime": regime,
        "regime_probs": {"bull_trend": 0.6, "uncertain": 0.3},
        "timeframe": "1h",
    }
    d.update(extra)
    return d


@pytest.fixture
def store():
    return CandidateStore(ttl_seconds=3600, max_active=5)


# ── S1: Creation and Basic Properties ──────────────────────────────────

class TestS1_Creation:
    def test_cs2_001_create_returns_staged_candidate(self, store):
        c = store.create_or_refresh(_cd())
        assert isinstance(c, StagedCandidate)
        assert c.state == CandidateState.CREATED

    def test_cs2_002_candidate_has_correct_fields(self, store):
        c = store.create_or_refresh(_cd(symbol="ETH/USDT", side="sell", score=0.82))
        assert c.symbol == "ETH/USDT"
        assert c.side == "sell"
        assert c.score == 0.82
        assert c.is_active is True
        assert c.is_terminal is False

    def test_cs2_003_candidate_id_unique(self, store):
        c1 = store.create_or_refresh(_cd(symbol="BTC/USDT"))
        time.sleep(0.002)
        c2 = store.create_or_refresh(_cd(symbol="ETH/USDT"))
        assert c1.candidate_id != c2.candidate_id

    def test_cs2_004_created_at_populated(self, store):
        before = time.time()
        c = store.create_or_refresh(_cd())
        after = time.time()
        assert before <= c.created_at <= after

    def test_cs2_005_active_count_increments(self, store):
        assert store.active_count == 0
        store.create_or_refresh(_cd(symbol="BTC/USDT"))
        assert store.active_count == 1
        store.create_or_refresh(_cd(symbol="ETH/USDT"))
        assert store.active_count == 2


# ── S2: Fingerprint Dedup and Refresh ──────────────────────────────────

class TestS2_Dedup:
    def test_cs2_010_same_fingerprint_refreshes(self, store):
        c1 = store.create_or_refresh(_cd(score=0.70))
        c2 = store.create_or_refresh(_cd(score=0.85))
        assert c1.candidate_id == c2.candidate_id
        assert c1.score == 0.85  # updated
        assert c1.refresh_count == 1
        assert store.active_count == 1

    def test_cs2_011_different_side_creates_new(self, store):
        store.create_or_refresh(_cd(side="buy"))
        store.create_or_refresh(_cd(side="sell"))
        assert store.active_count == 2

    def test_cs2_012_different_models_creates_new(self, store):
        store.create_or_refresh(_cd(models=["trend"]))
        store.create_or_refresh(_cd(models=["trend", "mean_reversion"]))
        assert store.active_count == 2

    def test_cs2_013_different_regime_creates_new(self, store):
        store.create_or_refresh(_cd(regime="bull_trend"))
        store.create_or_refresh(_cd(regime="ranging"))
        assert store.active_count == 2

    def test_cs2_014_different_symbol_creates_new(self, store):
        store.create_or_refresh(_cd(symbol="BTC/USDT"))
        store.create_or_refresh(_cd(symbol="ETH/USDT"))
        assert store.active_count == 2

    def test_cs2_015_refresh_resets_last_refreshed_at(self, store):
        c1 = store.create_or_refresh(_cd())
        first_refresh = c1.last_refreshed_at
        time.sleep(0.002)
        store.create_or_refresh(_cd())
        assert c1.last_refreshed_at > first_refresh

    def test_cs2_016_model_order_irrelevant_for_fingerprint(self):
        fp1 = make_fingerprint("BTC/USDT", "buy", ["trend", "mean_reversion"], "bull")
        fp2 = make_fingerprint("BTC/USDT", "buy", ["mean_reversion", "trend"], "bull")
        assert fp1 == fp2

    def test_cs2_017_regime_case_insensitive(self):
        fp1 = make_fingerprint("BTC/USDT", "buy", ["trend"], "Bull_Trend")
        fp2 = make_fingerprint("BTC/USDT", "buy", ["trend"], "bull_trend")
        assert fp1 == fp2

    def test_cs2_018_voided_candidate_allows_new_creation(self, store):
        c1 = store.create_or_refresh(_cd())
        store.void(c1.candidate_id, "test")
        c2 = store.create_or_refresh(_cd())
        assert c2.candidate_id != c1.candidate_id
        assert c2.state == CandidateState.CREATED


# ── S3: State Transitions ──────────────────────────────────────────────

class TestS3_Transitions:
    def test_cs2_020_confirm_from_created(self, store):
        c = store.create_or_refresh(_cd())
        ok = store.confirm(c.candidate_id, ltf_confirmation_price=87050.0, ltf_rsi=52.0)
        assert ok is True
        assert c.state == CandidateState.CONFIRMED
        assert c.confirmed_at is not None
        assert c.ltf_confirmation_price == 87050.0

    def test_cs2_021_execute_from_confirmed(self, store):
        c = store.create_or_refresh(_cd())
        store.confirm(c.candidate_id)
        ok = store.mark_executed(c.candidate_id)
        assert ok is True
        assert c.state == CandidateState.EXECUTED
        assert c.executed_at is not None

    def test_cs2_022_cannot_confirm_twice(self, store):
        c = store.create_or_refresh(_cd())
        store.confirm(c.candidate_id)
        ok = store.confirm(c.candidate_id)
        assert ok is False

    def test_cs2_023_cannot_execute_from_created(self, store):
        c = store.create_or_refresh(_cd())
        ok = store.mark_executed(c.candidate_id)
        assert ok is False
        assert c.state == CandidateState.CREATED

    def test_cs2_024_void_from_created(self, store):
        c = store.create_or_refresh(_cd())
        ok = store.void(c.candidate_id, "anti-churn: 15m RSI contradicts")
        assert ok is True
        assert c.state == CandidateState.VOIDED
        assert c.void_reason == "anti-churn: 15m RSI contradicts"

    def test_cs2_025_void_from_confirmed(self, store):
        c = store.create_or_refresh(_cd())
        store.confirm(c.candidate_id)
        ok = store.void(c.candidate_id, "crash_detected")
        assert ok is True
        assert c.state == CandidateState.VOIDED

    def test_cs2_026_cannot_void_executed(self, store):
        c = store.create_or_refresh(_cd())
        store.confirm(c.candidate_id)
        store.mark_executed(c.candidate_id)
        ok = store.void(c.candidate_id, "too late")
        assert ok is False
        assert c.state == CandidateState.EXECUTED

    def test_cs2_027_confirm_nonexistent_returns_false(self, store):
        assert store.confirm("fake_id") is False

    def test_cs2_028_execute_nonexistent_returns_false(self, store):
        assert store.mark_executed("fake_id") is False

    def test_cs2_029_full_lifecycle_created_confirmed_executed(self, store):
        c = store.create_or_refresh(_cd())
        assert c.state == CandidateState.CREATED
        store.confirm(c.candidate_id, ltf_rsi=48.0)
        assert c.state == CandidateState.CONFIRMED
        store.mark_executed(c.candidate_id)
        assert c.state == CandidateState.EXECUTED
        assert c.is_terminal is True
        assert c.is_active is False


# ── S4: Capacity Limits ────────────────────────────────────────────────

class TestS4_Capacity:
    def test_cs2_030_capacity_limit_enforced(self, store):
        for i in range(5):
            store.create_or_refresh(_cd(symbol=f"SYM{i}/USDT"))
        assert store.active_count == 5
        # 6th candidate should be voided
        c = store.create_or_refresh(_cd(symbol="OVERFLOW/USDT"))
        assert c.state == CandidateState.VOIDED
        assert c.void_reason == "capacity_exceeded"
        assert store.active_count == 5  # unchanged

    def test_cs2_031_voiding_frees_capacity(self, store):
        candidates = []
        for i in range(5):
            candidates.append(store.create_or_refresh(_cd(symbol=f"SYM{i}/USDT")))
        store.void(candidates[0].candidate_id, "test")
        assert store.active_count == 4
        c = store.create_or_refresh(_cd(symbol="NEW/USDT"))
        assert c.state == CandidateState.CREATED
        assert store.active_count == 5


# ── S5: Concurrency ────────────────────────────────────────────────────

class TestS5_Concurrency:
    def test_cs2_040_concurrent_creation_no_crash(self, store):
        errors = []
        def create_batch(start):
            try:
                for i in range(10):
                    store.create_or_refresh(_cd(symbol=f"SYM{start+i}/USDT"))
            except Exception as e:
                errors.append(e)

        store._max_active = 100  # increase for concurrency test
        threads = [threading.Thread(target=create_batch, args=(i*10,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(errors) == 0
        assert store.active_count == 40

    def test_cs2_041_concurrent_confirm_void_safe(self, store):
        store._max_active = 50
        candidates = [store.create_or_refresh(_cd(symbol=f"SYM{i}/USDT")) for i in range(20)]
        errors = []
        def confirm_half():
            try:
                for c in candidates[:10]:
                    store.confirm(c.candidate_id)
            except Exception as e:
                errors.append(e)
        def void_half():
            try:
                for c in candidates[10:]:
                    store.void(c.candidate_id, "concurrent void")
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=confirm_half)
        t2 = threading.Thread(target=void_half)
        t1.start(); t2.start()
        t1.join(); t2.join()
        assert len(errors) == 0


# ── S6: Expiry and Cleanup ─────────────────────────────────────────────

class TestS6_Expiry:
    def test_cs2_050_expire_stale_candidates(self):
        store = CandidateStore(ttl_seconds=1, max_active=10)
        c = store.create_or_refresh(_cd())
        assert c.state == CandidateState.CREATED
        time.sleep(1.1)
        expired = store.expire_stale()
        assert c.candidate_id in expired
        assert c.state == CandidateState.EXPIRED
        assert c.expired_at is not None

    def test_cs2_051_fresh_candidates_not_expired(self, store):
        store.create_or_refresh(_cd())
        expired = store.expire_stale()
        assert len(expired) == 0

    def test_cs2_052_cleanup_removes_old_terminal(self):
        store = CandidateStore(ttl_seconds=3600, max_active=10, retention_seconds=1)
        c = store.create_or_refresh(_cd())
        store.confirm(c.candidate_id)
        store.mark_executed(c.candidate_id)
        assert store.total_count == 1
        time.sleep(1.1)
        removed = store.cleanup_terminal()
        assert removed == 1
        assert store.total_count == 0

    def test_cs2_053_cleanup_preserves_active(self):
        store = CandidateStore(ttl_seconds=3600, max_active=10, retention_seconds=0)
        c = store.create_or_refresh(_cd())
        removed = store.cleanup_terminal()
        assert removed == 0
        assert store.total_count == 1


# ── S7: Query Methods ──────────────────────────────────────────────────

class TestS7_Queries:
    def test_cs2_060_get_created(self, store):
        c1 = store.create_or_refresh(_cd(symbol="BTC/USDT"))
        c2 = store.create_or_refresh(_cd(symbol="ETH/USDT"))
        store.confirm(c2.candidate_id)
        created = store.get_created()
        assert len(created) == 1
        assert created[0].symbol == "BTC/USDT"

    def test_cs2_061_get_confirmed(self, store):
        c = store.create_or_refresh(_cd())
        store.confirm(c.candidate_id)
        confirmed = store.get_confirmed()
        assert len(confirmed) == 1
        assert confirmed[0].state == CandidateState.CONFIRMED

    def test_cs2_062_get_by_id(self, store):
        c = store.create_or_refresh(_cd())
        found = store.get_by_id(c.candidate_id)
        assert found is c

    def test_cs2_063_get_by_id_not_found(self, store):
        assert store.get_by_id("nonexistent") is None

    def test_cs2_064_get_by_symbol(self, store):
        store.create_or_refresh(_cd(symbol="BTC/USDT"))
        store.create_or_refresh(_cd(symbol="ETH/USDT"))
        btc = store.get_by_symbol("BTC/USDT")
        assert len(btc) == 1
        assert btc[0].symbol == "BTC/USDT"

    def test_cs2_065_get_active_all(self, store):
        store.create_or_refresh(_cd(symbol="BTC/USDT"))
        c2 = store.create_or_refresh(_cd(symbol="ETH/USDT"))
        store.confirm(c2.candidate_id)
        active = store.get_active()
        assert len(active) == 2  # CREATED + CONFIRMED are both active


# ── S8: Transition Log ─────────────────────────────────────────────────

class TestS8_AuditLog:
    def test_cs2_070_creation_logged(self, store):
        store.create_or_refresh(_cd())
        log = store.get_transition_log()
        assert len(log) == 1
        assert log[0]["event"] == "CREATED"
        assert log[0]["symbol"] == "BTC/USDT"

    def test_cs2_071_full_lifecycle_logged(self, store):
        c = store.create_or_refresh(_cd())
        store.confirm(c.candidate_id)
        store.mark_executed(c.candidate_id)
        log = store.get_transition_log()
        events = [e["event"] for e in log]
        assert events == ["CREATED", "CONFIRMED", "EXECUTED"]

    def test_cs2_072_refresh_logged(self, store):
        store.create_or_refresh(_cd())
        store.create_or_refresh(_cd())
        log = store.get_transition_log()
        events = [e["event"] for e in log]
        assert events == ["CREATED", "REFRESHED"]

    def test_cs2_073_log_limit(self, store):
        log = store.get_transition_log(limit=2)
        assert len(log) <= 2

    def test_cs2_074_void_reason_in_log(self, store):
        c = store.create_or_refresh(_cd())
        store.void(c.candidate_id, "anti-churn: RSI 78 > 75")
        log = store.get_transition_log()
        void_entry = [e for e in log if e["event"] == "VOIDED"][0]
        assert "anti-churn" in void_entry["detail"]


# ── S9: Edge Cases ─────────────────────────────────────────────────────

class TestS9_EdgeCases:
    def test_cs2_080_empty_models_fired(self, store):
        c = store.create_or_refresh(_cd(models=[]))
        assert c.state == CandidateState.CREATED

    def test_cs2_081_none_models_fired(self, store):
        c = store.create_or_refresh(_cd(models=None))
        assert c.state == CandidateState.CREATED

    def test_cs2_082_empty_regime(self, store):
        c = store.create_or_refresh(_cd(regime=""))
        assert c.state == CandidateState.CREATED

    def test_cs2_083_age_property(self, store):
        c = store.create_or_refresh(_cd())
        time.sleep(0.05)
        assert c.age_seconds >= 0.04

    def test_cs2_084_ttl_setter_minimum(self, store):
        store.ttl_seconds = 10
        assert store.ttl_seconds == 60  # clamped to minimum 60s

    def test_cs2_085_max_active_setter_minimum(self, store):
        store.max_active = 0
        assert store.max_active == 1  # clamped to minimum 1


# ── S10: Config Integration ────────────────────────────────────────────

class TestS10_Config:
    def test_cs2_090_default_config_has_staged_candidates_section(self):
        from config.settings import DEFAULT_CONFIG
        sc = DEFAULT_CONFIG.get("staged_candidates", {})
        assert sc.get("enabled") is True
        assert sc.get("ttl_seconds") == 10800
        assert sc.get("max_active") == 20
        assert sc.get("retention_seconds") == 86400

    def test_cs2_091_singleton_returns_same_instance(self):
        import core.scanning.candidate_store as mod
        old = mod._store
        try:
            mod._store = None  # force re-creation
            s1 = get_candidate_store()
            s2 = get_candidate_store()
            assert s1 is s2
        finally:
            mod._store = old

    def test_cs2_092_fingerprint_function_exported(self):
        fp = make_fingerprint("X/USDT", "buy", ["a", "b"], "bull")
        assert isinstance(fp, tuple)
        assert len(fp) == 4
