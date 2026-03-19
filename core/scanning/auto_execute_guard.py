# ============================================================
# NEXUS TRADER — Auto-Execute Safeguard Logic
#
# Pure Python module: no Qt, no PySide6.
# Extracted so the guard logic is unit-testable in headless
# CI environments without a display.
#
# Called by IDSSScannerTab._try_auto_execute() after each
# IDSS scan cycle when auto-execute is enabled.
# ============================================================
from __future__ import annotations

import logging
import time
from datetime import datetime, date, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# Timeframe string → duration in seconds
TF_SECONDS: dict[str, int] = {
    "1m":  60,    "3m":  180,   "5m":  300,   "15m": 900,
    "30m": 1800,  "1h":  3600,  "2h":  7200,  "4h":  14400,
    "6h":  21600, "8h":  28800, "12h": 43200, "1d":  86400,
}

# Rejection reason codes returned by check_candidate()
REJECT_NO_SIGNAL     = "no_signal"
REJECT_STALE         = "stale_age"
REJECT_COOLDOWN      = "cooldown"
REJECT_DUPLICATE     = "duplicate_symbol"
REJECT_POSITION_LIMIT = "position_limit"
REJECT_DRAWDOWN_HALT = "drawdown_halt"
PASS                 = "pass"


class AutoExecuteState:
    """
    Mutable state shared across one scan batch.
    Holds daily counter, cooldown timestamps, and today's date for rollover.
    """

    def __init__(self, cooldown_seconds: int = 30):
        self.cooldown_seconds:   int              = cooldown_seconds
        self._last_exec:         dict[str, float] = {}   # symbol → monotonic time
        self._today_count:       int              = 0
        self._today_date:        date             = date.today()

    # ── public properties ─────────────────────────────────

    @property
    def today_count(self) -> int:
        return self._today_count

    def record_execution(self, symbol: str) -> None:
        """Call after a successful auto-execution for *symbol*."""
        self._last_exec[symbol] = time.monotonic()
        self._today_count      += 1

    def reset_if_new_day(self) -> None:
        """Clear stale state when the calendar date rolls over."""
        today = date.today()
        if today != self._today_date:
            self._today_count = 0
            self._today_date  = today
            self._last_exec.clear()
            logger.info("AutoExecuteState: daily counter reset for %s", today)

    def in_cooldown(self, symbol: str) -> bool:
        """Return True if *symbol* was executed within the cooldown window."""
        last = self._last_exec.get(symbol, 0.0)
        return (time.monotonic() - last) < self.cooldown_seconds

    def cooldown_remaining(self, symbol: str) -> int:
        """Seconds remaining in cooldown for *symbol* (0 if not in cooldown)."""
        last = self._last_exec.get(symbol, 0.0)
        elapsed = time.monotonic() - last
        return max(0, int(self.cooldown_seconds - elapsed))


def candidate_is_eligible(candidate: dict) -> bool:
    """
    Return True if the candidate has a confirmed direction and real models.
    This is the first gate — candidates without a side or models are display-only rows.
    """
    if candidate.get("_no_signal"):
        return False
    if not candidate.get("models_fired"):
        return False
    if not candidate.get("side"):
        return False
    return True


def candidate_age_ok(candidate: dict, timeframe: str) -> bool:
    """
    Return True if the candidate was generated within 1× the timeframe duration.
    Stale candidates (born in a previous scan cycle) are skipped to avoid
    entering trades on old signals.
    """
    gen_iso = candidate.get("generated_at", "")
    if not gen_iso:
        return True   # no timestamp → allow through (defensive)
    try:
        gen_dt = datetime.fromisoformat(gen_iso)
        if gen_dt.tzinfo is None:
            gen_dt = gen_dt.replace(tzinfo=timezone.utc)
        age_secs = (datetime.now(timezone.utc) - gen_dt).total_seconds()
        tf_secs  = TF_SECONDS.get(timeframe, 3600)
        return age_secs <= tf_secs
    except Exception:
        return True   # unparseable timestamp → allow through


def _condition_fingerprint(side: str, models_fired: list, regime: str) -> tuple:
    """Build a hashable condition fingerprint: (side, frozenset(models), regime_lower)."""
    return (side, frozenset(models_fired or []), (regime or "").lower())


def _has_duplicate_condition(candidate: dict, open_positions: list[dict]) -> bool:
    """
    Return True if *candidate* would duplicate the condition of an
    already-open position for the same symbol.

    A condition is considered the same when (side, models_fired set, regime) match.
    """
    sym = candidate.get("symbol", "")
    cand_fp = _condition_fingerprint(
        candidate.get("side", ""),
        candidate.get("models_fired", []),
        candidate.get("regime", ""),
    )
    for pos in open_positions:
        if pos.get("symbol") != sym:
            continue
        pos_fp = _condition_fingerprint(
            pos.get("side", ""),
            pos.get("models_fired", []),
            pos.get("regime", ""),
        )
        if cand_fp == pos_fp:
            return True
    return False


def check_candidate(
    candidate:      dict,
    timeframe:      str,
    open_positions: list[dict],
    n_open:         int,
    max_pos:        int,
    drawdown_pct:   float,
    max_dd_pct:     float,
    state:          AutoExecuteState,
    # Backward-compat: old callers may still pass open_symbols as kwarg
    open_symbols:   set[str] | None = None,
) -> str:
    """
    Run all safeguard checks for a single candidate.

    Returns one of the REJECT_* constants or PASS.
    The caller is responsible for calling state.record_execution() on success.
    """
    sym = candidate.get("symbol", "")

    if not candidate_is_eligible(candidate):
        return REJECT_NO_SIGNAL

    if drawdown_pct >= max_dd_pct:
        return REJECT_DRAWDOWN_HALT

    if n_open >= max_pos:
        return REJECT_POSITION_LIMIT

    # ── Condition-based duplicate check ──────────────────────────
    # Reject if an open position for the same symbol already has
    # the exact same (side, models_fired set, regime).
    if _has_duplicate_condition(candidate, open_positions):
        return REJECT_DUPLICATE

    if not candidate_age_ok(candidate, timeframe):
        return REJECT_STALE

    if state.in_cooldown(sym):
        return REJECT_COOLDOWN

    return PASS


def run_batch(
    candidates:     list[dict],
    timeframe:      str,
    open_positions: list[dict] | None = None,
    drawdown_pct:   float = 0.0,
    max_dd_pct:     float = 15.0,
    max_pos:        int   = 50,
    state:          AutoExecuteState | None = None,
    # Backward-compat: old callers may still pass open_symbols as kwarg
    open_symbols:   set[str] | None = None,
) -> list[dict]:
    """
    Evaluate the full batch of candidates and return those that should be
    auto-executed.  Updates *state* (record_execution) for each approved one.
    Callers should still call state.reset_if_new_day() before invoking this.

    Limit: at most ONE approved candidate per PAIR per scan cycle.
    Multiple pairs can each get one trade in the same cycle.

    Returns a list of candidate dicts that passed all checks (in order).
    """
    if state is None:
        state = AutoExecuteState()

    # Backward compat: if caller passed open_symbols but not open_positions,
    # convert to minimal position dicts (condition dedup will be skipped since
    # the dicts lack models_fired/side/regime — but per-symbol limit still works).
    if open_positions is None:
        if open_symbols is not None:
            open_positions = [{"symbol": s} for s in open_symbols]
        else:
            open_positions = []

    # Drawdown halt blocks the whole batch immediately
    if drawdown_pct >= max_dd_pct:
        logger.info(
            "AutoExecuteGuard: drawdown halt (%.1f%% ≥ %.1f%%) — skipping all",
            drawdown_pct, max_dd_pct,
        )
        return []

    # Roll over daily counter / cooldowns when the date changes
    state.reset_if_new_day()

    eligible: list[dict] = []
    # Track positions added within this batch for condition dedup
    local_positions = list(open_positions)
    # Track which symbols have been approved THIS cycle (one trade per pair)
    approved_this_cycle: set[str] = set()

    for c in candidates:
        sym = c.get("symbol", "")

        # One trade per pair per cycle — skip if this pair was already approved
        if sym in approved_this_cycle:
            logger.debug(
                "AutoExecuteGuard: SKIPPED %s (already approved this cycle)", sym,
            )
            continue

        n_open = len(local_positions)
        verdict = check_candidate(
            candidate      = c,
            timeframe      = timeframe,
            open_positions = local_positions,
            n_open         = n_open,
            max_pos        = max_pos,
            drawdown_pct   = drawdown_pct,
            max_dd_pct     = max_dd_pct,
            state          = state,
        )

        if verdict == PASS:
            eligible.append(c)
            state.record_execution(sym)
            approved_this_cycle.add(sym)
            # Add to local positions so next candidate sees this as "open"
            local_positions.append({
                "symbol":       sym,
                "side":         c.get("side", ""),
                "models_fired": c.get("models_fired", []),
                "regime":       c.get("regime", ""),
            })
            logger.info(
                "AutoExecuteGuard: APPROVED %s %s (score=%.3f)",
                sym, c.get("side", "?"), c.get("score", 0.0),
            )
        elif verdict == REJECT_POSITION_LIMIT:
            logger.info(
                "AutoExecuteGuard: position limit reached — stopping batch",
            )
            break   # no point checking remaining candidates
        else:
            logger.debug(
                "AutoExecuteGuard: REJECTED %s (%s)", sym, verdict,
            )

    return eligible
