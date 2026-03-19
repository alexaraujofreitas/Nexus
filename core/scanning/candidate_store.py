"""
Staged Candidate Store — lifecycle manager for HTF signal candidates.

Candidates progress through a strict state machine:

    CREATED  ──→  CONFIRMED  ──→  EXECUTED
       │              │
       ├──→  EXPIRED  │
       │              │
       └──→  VOIDED ←─┘

State transitions:
  - CREATED:    HTF (1H) pipeline approves a candidate via ConfluenceScorer + RiskGate.
  - CONFIRMED:  LTF (15m) confirmation module validates the HTF signal.
  - EXECUTED:   Trade submitted to PaperExecutor (or live executor).
  - EXPIRED:    Candidate exceeded its TTL without confirmation.
  - VOIDED:     LTF data actively contradicts the HTF signal direction.

Dedup:
  Each candidate has a fingerprint = (symbol, side, frozenset(models_fired), regime).
  If a new candidate has the same fingerprint as an active (CREATED/CONFIRMED) candidate,
  the existing candidate is refreshed (timestamp updated, TTL reset) rather than
  creating a duplicate.

Thread safety:
  All mutations use a threading.Lock.  The store is safe to call from both the
  1H scan worker thread and the 15m confirmation timer thread.

Configuration (all via settings.yaml / config.yaml):
  - staged_candidates.ttl_seconds:  max age before expiry (default 10800 = 3 hours)
  - staged_candidates.max_active:   capacity limit (default 20)
  - staged_candidates.enabled:      master toggle (default True)
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


# ── Candidate States ────────────────────────────────────────────────────

class CandidateState(Enum):
    CREATED   = "CREATED"
    CONFIRMED = "CONFIRMED"
    EXECUTED  = "EXECUTED"
    EXPIRED   = "EXPIRED"
    VOIDED    = "VOIDED"


# Terminal states — candidates in these states are immutable and will be
# garbage-collected after the retention period.
_TERMINAL = frozenset({CandidateState.EXECUTED, CandidateState.EXPIRED, CandidateState.VOIDED})

# Active states — candidates that can still be confirmed or executed.
_ACTIVE = frozenset({CandidateState.CREATED, CandidateState.CONFIRMED})


# ── Data Model ──────────────────────────────────────────────────────────

@dataclass
class StagedCandidate:
    """One staged candidate with full lifecycle tracking."""

    # Identity
    candidate_id: str                    # Unique ID: "{symbol}_{side}_{created_epoch_ms}"
    symbol: str
    side: str                            # "buy" or "sell"
    fingerprint: tuple                   # (symbol, side, frozenset(models), regime)

    # Signal data (from HTF pipeline)
    score: float                         # Confluence score (0–1)
    entry_price: float                   # HTF model entry price (ATR-buffered)
    stop_loss_price: float
    take_profit_price: float
    models_fired: list[str]              # Sub-model names
    regime: str                          # Regime at creation
    regime_probs: dict = field(default_factory=dict)
    htf_timeframe: str = "1h"
    raw_candidate_dict: dict = field(default_factory=dict)  # Full OrderCandidate.to_dict()

    # Lifecycle
    state: CandidateState = CandidateState.CREATED
    created_at: float = 0.0              # epoch seconds
    confirmed_at: Optional[float] = None
    executed_at: Optional[float] = None
    expired_at: Optional[float] = None
    voided_at: Optional[float] = None

    # LTF confirmation data (populated by confirmation module)
    ltf_confirmation_price: Optional[float] = None   # 15m close at confirmation
    ltf_rsi: Optional[float] = None
    ltf_ema_aligned: Optional[bool] = None
    ltf_volume_ratio: Optional[float] = None
    void_reason: Optional[str] = None

    # Refresh tracking
    refresh_count: int = 0               # How many times the HTF pipeline re-created this
    last_refreshed_at: float = 0.0

    @property
    def is_active(self) -> bool:
        return self.state in _ACTIVE

    @property
    def is_terminal(self) -> bool:
        return self.state in _TERMINAL

    @property
    def age_seconds(self) -> float:
        return time.time() - self.created_at


# ── Fingerprint Utility ─────────────────────────────────────────────────

def make_fingerprint(
    symbol: str,
    side: str,
    models_fired: list[str] | None,
    regime: str,
) -> tuple:
    """Create an order-invariant, case-insensitive dedup fingerprint."""
    return (
        symbol,
        side.lower(),
        frozenset(models_fired or []),
        (regime or "").lower(),
    )


# ── Candidate Store ────────────────────────────────────────────────────

import itertools as _itertools
_id_counter = _itertools.count(1)


class CandidateStore:
    """Thread-safe store for staged candidates with lifecycle management."""

    def __init__(
        self,
        ttl_seconds: int = 10800,     # 3 hours default
        max_active: int = 20,
        retention_seconds: int = 86400,  # keep terminal candidates 24h for audit
    ):
        self._ttl_seconds = ttl_seconds
        self._max_active = max_active
        self._retention_seconds = retention_seconds
        self._candidates: dict[str, StagedCandidate] = {}  # id → candidate
        self._fingerprint_index: dict[tuple, str] = {}     # fingerprint → id
        self._lock = threading.Lock()
        self._transition_log: list[dict] = []   # audit trail

    # ── Configuration ────────────────────────────────────────────────

    @property
    def ttl_seconds(self) -> int:
        return self._ttl_seconds

    @ttl_seconds.setter
    def ttl_seconds(self, value: int) -> None:
        self._ttl_seconds = max(60, value)  # minimum 1 minute

    @property
    def max_active(self) -> int:
        return self._max_active

    @max_active.setter
    def max_active(self, value: int) -> None:
        self._max_active = max(1, value)

    # ── Core Operations ──────────────────────────────────────────────

    def create_or_refresh(self, candidate_dict: dict) -> StagedCandidate:
        """Create a new staged candidate or refresh an existing one with the same fingerprint.

        Parameters
        ----------
        candidate_dict : dict
            OrderCandidate.to_dict() output from the HTF pipeline.

        Returns
        -------
        StagedCandidate
            The created or refreshed candidate.
        """
        symbol = candidate_dict.get("symbol", "?")
        side = candidate_dict.get("side", "buy")
        models = candidate_dict.get("models_fired", [])
        regime = candidate_dict.get("regime", "")
        fp = make_fingerprint(symbol, side, models, regime)

        with self._lock:
            # Check for existing active candidate with same fingerprint
            existing_id = self._fingerprint_index.get(fp)
            if existing_id and existing_id in self._candidates:
                existing = self._candidates[existing_id]
                if existing.is_active:
                    # Refresh: update score/price, reset TTL
                    existing.score = candidate_dict.get("score", existing.score)
                    existing.entry_price = candidate_dict.get("entry_price", existing.entry_price)
                    existing.stop_loss_price = candidate_dict.get("stop_loss_price", existing.stop_loss_price)
                    existing.take_profit_price = candidate_dict.get("take_profit_price", existing.take_profit_price)
                    existing.regime_probs = candidate_dict.get("regime_probs", existing.regime_probs)
                    existing.raw_candidate_dict = candidate_dict
                    existing.refresh_count += 1
                    existing.last_refreshed_at = time.time()
                    self._log_transition(existing, "REFRESHED",
                                         f"refresh #{existing.refresh_count}, score={existing.score:.3f}")
                    return existing

            # Check capacity
            active_count = sum(1 for c in self._candidates.values() if c.is_active)
            if active_count >= self._max_active:
                logger.warning(
                    "CandidateStore: max active candidates (%d) reached — "
                    "rejecting %s %s (score=%.3f)",
                    self._max_active, symbol, side,
                    candidate_dict.get("score", 0),
                )
                # Still return a candidate object but in VOIDED state
                c = self._make_candidate(candidate_dict, fp)
                c.state = CandidateState.VOIDED
                c.voided_at = time.time()
                c.void_reason = "capacity_exceeded"
                self._log_transition(c, "VOIDED", "capacity exceeded")
                return c

            # Create new candidate
            c = self._make_candidate(candidate_dict, fp)
            self._candidates[c.candidate_id] = c
            self._fingerprint_index[fp] = c.candidate_id
            self._log_transition(c, "CREATED",
                                 f"score={c.score:.3f}, models={c.models_fired}, regime={c.regime}")
            return c

    def confirm(self, candidate_id: str, **ltf_data) -> bool:
        """Transition a candidate from CREATED → CONFIRMED.

        Parameters
        ----------
        candidate_id : str
            The candidate to confirm.
        **ltf_data
            LTF confirmation data (ltf_confirmation_price, ltf_rsi,
            ltf_ema_aligned, ltf_volume_ratio).

        Returns
        -------
        bool
            True if the transition succeeded.
        """
        with self._lock:
            c = self._candidates.get(candidate_id)
            if c is None:
                logger.warning("CandidateStore: confirm() — candidate %s not found", candidate_id)
                return False
            if c.state != CandidateState.CREATED:
                logger.debug("CandidateStore: confirm() — %s is %s, not CREATED", candidate_id, c.state.value)
                return False

            c.state = CandidateState.CONFIRMED
            c.confirmed_at = time.time()
            c.ltf_confirmation_price = ltf_data.get("ltf_confirmation_price")
            c.ltf_rsi = ltf_data.get("ltf_rsi")
            c.ltf_ema_aligned = ltf_data.get("ltf_ema_aligned")
            c.ltf_volume_ratio = ltf_data.get("ltf_volume_ratio")
            self._log_transition(c, "CONFIRMED",
                                 f"ltf_price={c.ltf_confirmation_price}, rsi={c.ltf_rsi}, "
                                 f"ema_aligned={c.ltf_ema_aligned}, vol_ratio={c.ltf_volume_ratio}")
            return True

    def mark_executed(self, candidate_id: str) -> bool:
        """Transition a candidate from CONFIRMED → EXECUTED."""
        with self._lock:
            c = self._candidates.get(candidate_id)
            if c is None:
                return False
            if c.state != CandidateState.CONFIRMED:
                logger.debug("CandidateStore: mark_executed() — %s is %s, not CONFIRMED",
                             candidate_id, c.state.value)
                return False

            c.state = CandidateState.EXECUTED
            c.executed_at = time.time()
            # Remove from fingerprint index so future signals can create new candidates
            self._fingerprint_index.pop(c.fingerprint, None)
            self._log_transition(c, "EXECUTED",
                                 f"symbol={c.symbol}, side={c.side}, "
                                 f"entry={c.ltf_confirmation_price or c.entry_price:.4f}")
            return True

    def void(self, candidate_id: str, reason: str = "") -> bool:
        """Transition a candidate from CREATED or CONFIRMED → VOIDED."""
        with self._lock:
            c = self._candidates.get(candidate_id)
            if c is None:
                return False
            if not c.is_active:
                return False

            c.state = CandidateState.VOIDED
            c.voided_at = time.time()
            c.void_reason = reason
            self._fingerprint_index.pop(c.fingerprint, None)
            self._log_transition(c, "VOIDED", f"reason={reason}")
            return True

    def expire_stale(self) -> list[str]:
        """Expire all active candidates that have exceeded their TTL.

        Returns list of expired candidate IDs.
        """
        now = time.time()
        expired_ids = []
        with self._lock:
            for cid, c in self._candidates.items():
                if c.is_active and (now - c.created_at) > self._ttl_seconds:
                    c.state = CandidateState.EXPIRED
                    c.expired_at = now
                    self._fingerprint_index.pop(c.fingerprint, None)
                    expired_ids.append(cid)
                    self._log_transition(c, "EXPIRED",
                                         f"age={now - c.created_at:.0f}s > ttl={self._ttl_seconds}s")
        return expired_ids

    def cleanup_terminal(self) -> int:
        """Remove terminal candidates older than retention_seconds.

        Returns count of removed candidates.
        """
        now = time.time()
        to_remove = []
        with self._lock:
            for cid, c in self._candidates.items():
                if c.is_terminal:
                    terminal_ts = c.executed_at or c.expired_at or c.voided_at or c.created_at
                    if (now - terminal_ts) > self._retention_seconds:
                        to_remove.append(cid)
            for cid in to_remove:
                del self._candidates[cid]
            # Also trim the transition log
            if len(self._transition_log) > 1000:
                self._transition_log = self._transition_log[-500:]
        return len(to_remove)

    # ── Queries ──────────────────────────────────────────────────────

    def get_active(self, state: Optional[CandidateState] = None) -> list[StagedCandidate]:
        """Return all active candidates, optionally filtered by state."""
        with self._lock:
            if state:
                return [c for c in self._candidates.values() if c.state == state]
            return [c for c in self._candidates.values() if c.is_active]

    def get_created(self) -> list[StagedCandidate]:
        """Convenience: all candidates awaiting LTF confirmation."""
        return self.get_active(CandidateState.CREATED)

    def get_confirmed(self) -> list[StagedCandidate]:
        """Convenience: all candidates ready for execution."""
        return self.get_active(CandidateState.CONFIRMED)

    def get_by_id(self, candidate_id: str) -> Optional[StagedCandidate]:
        with self._lock:
            return self._candidates.get(candidate_id)

    def get_by_symbol(self, symbol: str) -> list[StagedCandidate]:
        with self._lock:
            return [c for c in self._candidates.values() if c.symbol == symbol and c.is_active]

    @property
    def active_count(self) -> int:
        with self._lock:
            return sum(1 for c in self._candidates.values() if c.is_active)

    @property
    def total_count(self) -> int:
        with self._lock:
            return len(self._candidates)

    def get_transition_log(self, limit: int = 50) -> list[dict]:
        """Return recent transition log entries for audit/dashboard display."""
        with self._lock:
            return list(self._transition_log[-limit:])

    # ── Internal Helpers ─────────────────────────────────────────────

    def _make_candidate(self, cd: dict, fp: tuple) -> StagedCandidate:
        now = time.time()
        seq = next(_id_counter)
        cid = f"{cd.get('symbol', '?')}_{cd.get('side', '?')}_{int(now * 1000)}_{seq}"
        return StagedCandidate(
            candidate_id=cid,
            symbol=cd.get("symbol", "?"),
            side=cd.get("side", "buy"),
            fingerprint=fp,
            score=cd.get("score", 0.0),
            entry_price=cd.get("entry_price", 0.0),
            stop_loss_price=cd.get("stop_loss_price", 0.0),
            take_profit_price=cd.get("take_profit_price", 0.0),
            models_fired=cd.get("models_fired", []),
            regime=cd.get("regime", ""),
            regime_probs=cd.get("regime_probs", {}),
            htf_timeframe=cd.get("timeframe", "1h"),
            raw_candidate_dict=cd,
            state=CandidateState.CREATED,
            created_at=now,
            last_refreshed_at=now,
        )

    def _log_transition(self, c: StagedCandidate, event: str, detail: str = "") -> None:
        entry = {
            "ts": time.time(),
            "candidate_id": c.candidate_id,
            "symbol": c.symbol,
            "side": c.side,
            "event": event,
            "state": c.state.value,
            "detail": detail,
        }
        self._transition_log.append(entry)
        logger.info(
            "CandidateStore: [%s] %s %s %s — %s",
            event, c.symbol, c.side, c.state.value, detail,
        )


# ── Module Singleton ─────────────────────────────────────────────────

_store: Optional[CandidateStore] = None
_store_lock = threading.Lock()


def get_candidate_store() -> CandidateStore:
    """Return the module-level CandidateStore singleton, creating it on first use."""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                try:
                    from config.settings import settings as _s
                    ttl = int(_s.get("staged_candidates.ttl_seconds", 10800))
                    max_active = int(_s.get("staged_candidates.max_active", 20))
                except Exception:
                    ttl = 10800
                    max_active = 20
                _store = CandidateStore(ttl_seconds=ttl, max_active=max_active)
    return _store
