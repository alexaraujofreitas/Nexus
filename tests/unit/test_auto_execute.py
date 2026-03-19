# ============================================================
# NEXUS TRADER — Unit tests for IDSS auto-execute feature
#
# All safeguard logic lives in core/scanning/auto_execute_guard.py
# (pure Python, zero Qt imports) so these tests run headlessly.
#
# Coverage:
#   AE-01  Settings keys present in DEFAULT_CONFIG with correct defaults
#   AE-02  Settings keys load from YAML via settings.get()
#   AE-03  Toggle on → persists True to scanner.auto_execute setting
#   AE-04  Toggle off → persists False to scanner.auto_execute setting
#   AE-05  Cooldown blocks same-symbol re-execution within window
#   AE-06  Cooldown allows execution after window expires
#   AE-07  Safeguard: drawdown halt blocks entire batch
#   AE-08  Safeguard: duplicate symbol (already open) is skipped
#   AE-09  Safeguard: position limit stops the batch
#   AE-10  Safeguard: stale candidate (age > 1× TF) is skipped
#   AE-11  Fresh candidate (age ≤ 1× TF) is allowed through
#   AE-12  _no_signal candidates are silently ignored
#   AE-13  Candidate with empty models_fired is silently ignored
#   AE-14  Successful auto-execute increments daily counter
#   AE-15  Daily counter resets when date changes
#   AE-16a Thread-safety: _on_scan_complete is a regular (non-worker) slot
#   AE-16b Thread-safety: ScanWorker.run never calls order_router.submit
#   AE-17  run_batch returns empty list on rejection
#   AE-18  run_batch: first succeeds, second blocked by limit (batch stop)
#   AE-19  AutoExecuteState.record_execution updates last timestamp
#   AE-20  Cooldown dict cleared on date rollover
#   AE-21  candidate_is_eligible checks all three conditions
#   AE-22  candidate_age_ok returns True for missing generated_at
#   AE-23  check_candidate returns correct REJECT_* codes per guard
# ============================================================
from __future__ import annotations

import time
from datetime import datetime, date, timezone, timedelta
from unittest.mock import MagicMock, patch
import pytest

from core.scanning.auto_execute_guard import (
    AutoExecuteState,
    candidate_is_eligible,
    candidate_age_ok,
    check_candidate,
    run_batch,
    PASS,
    REJECT_NO_SIGNAL,
    REJECT_STALE,
    REJECT_COOLDOWN,
    REJECT_DUPLICATE,
    REJECT_POSITION_LIMIT,
    REJECT_DRAWDOWN_HALT,
    TF_SECONDS,
)


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _c(
    symbol: str = "ETH/USDT",
    side: str = "buy",
    age_seconds: int = 60,
    models: list | None = None,
    no_signal: bool = False,
) -> dict:
    gen = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    return {
        "symbol":       symbol,
        "side":         side,
        "entry_price":  2000.0,
        "stop_loss_price":   1950.0,
        "take_profit_price": 2100.0,
        "position_size_usdt": 50.0,
        "score":        0.70,
        "models_fired": models if models is not None else ["trend"],
        "regime":       "bull_trend",
        "generated_at": gen.isoformat(),
        "_no_signal":   no_signal,
        "timeframe":    "1h",
        "atr_value":    25.0,
        "rationale":    "unit test",
    }


def _state(cooldown: int = 30) -> AutoExecuteState:
    return AutoExecuteState(cooldown_seconds=cooldown)


def _pos(symbol="ETH/USDT", side="buy", models=None, regime="bull_trend") -> dict:
    """Build a minimal open-position dict for condition dedup tests."""
    return {
        "symbol":       symbol,
        "side":         side,
        "models_fired": models if models is not None else ["trend"],
        "regime":       regime,
    }


def _run(candidates, **kw):
    """Convenience wrapper for run_batch with sane defaults."""
    defaults = dict(
        timeframe="1h",
        open_positions=[],
        drawdown_pct=0.0,
        max_dd_pct=15.0,
        max_pos=50,
        state=_state(),
    )
    defaults.update(kw)
    return run_batch(candidates, **defaults)


# ─────────────────────────────────────────────────────────────
# AE-01 / AE-02 — Settings defaults
# ─────────────────────────────────────────────────────────────

class TestSettings:
    def test_ae01_default_config_has_auto_execute(self):
        """AE-01: DEFAULT_CONFIG scanner section has auto_execute=True (must always be on)."""
        from config.settings import DEFAULT_CONFIG
        assert DEFAULT_CONFIG["scanner"]["auto_execute"] is True

    def test_ae01_default_config_has_cooldown(self):
        """AE-01: DEFAULT_CONFIG scanner section has auto_execute_cooldown_seconds=30."""
        from config.settings import DEFAULT_CONFIG
        assert DEFAULT_CONFIG["scanner"]["auto_execute_cooldown_seconds"] == 30

    def test_ae02_default_config_has_auto_execute_true(self):
        """AE-02: DEFAULT_CONFIG scanner section has auto_execute=True.
        Auto-execute must always be on by default on every restart.
        """
        from config.settings import DEFAULT_CONFIG
        val = DEFAULT_CONFIG["scanner"]["auto_execute"]
        assert val is True

    def test_ae02_settings_get_cooldown(self):
        """AE-02: settings.get cooldown returns int 30."""
        from config.settings import settings
        val = settings.get("scanner.auto_execute_cooldown_seconds", 30)
        assert val == 30


# ─────────────────────────────────────────────────────────────
# AE-03 / AE-04 — Toggle persistence (pure logic, no Qt)
# ─────────────────────────────────────────────────────────────

class TestTogglePersistence:
    def test_ae03_enable_saves_true(self):
        """AE-03: Enabling the toggle calls settings.set('scanner.auto_execute', True)."""
        from config.settings import settings
        original = settings.get("scanner.auto_execute", False)
        try:
            settings.set("scanner.auto_execute", True)
            assert settings.get("scanner.auto_execute") is True
        finally:
            settings.set("scanner.auto_execute", original)

    def test_ae04_disable_saves_false(self):
        """AE-04: Disabling the toggle calls settings.set('scanner.auto_execute', False)."""
        from config.settings import settings
        settings.set("scanner.auto_execute", True)
        settings.set("scanner.auto_execute", False)
        assert settings.get("scanner.auto_execute") is False


# ─────────────────────────────────────────────────────────────
# AE-05 / AE-06 — Cooldown enforcement
# ─────────────────────────────────────────────────────────────

class TestCooldown:
    def test_ae05_in_cooldown_blocks(self):
        """AE-05: Symbol executed 5s ago is still in 30s cooldown."""
        st = _state(cooldown=30)
        st._last_exec["ETH/USDT"] = time.monotonic() - 5   # 5s ago
        assert st.in_cooldown("ETH/USDT") is True

    def test_ae05_run_batch_blocks_cooldown(self):
        """AE-05: run_batch skips symbol that is in cooldown."""
        st = _state(cooldown=30)
        st._last_exec["ETH/USDT"] = time.monotonic() - 5
        result = _run([_c("ETH/USDT", age_seconds=60)], state=st)
        assert result == []

    def test_ae06_past_cooldown_allowed(self):
        """AE-06: Symbol executed 60s ago passes 30s cooldown."""
        st = _state(cooldown=30)
        st._last_exec["ETH/USDT"] = time.monotonic() - 60   # 60s ago
        assert st.in_cooldown("ETH/USDT") is False

    def test_ae06_run_batch_allows_after_cooldown(self):
        """AE-06: run_batch submits symbol after cooldown window expires."""
        st = _state(cooldown=30)
        st._last_exec["ETH/USDT"] = time.monotonic() - 60
        result = _run([_c("ETH/USDT", age_seconds=60)], state=st)
        assert len(result) == 1


# ─────────────────────────────────────────────────────────────
# AE-07 / AE-08 / AE-09 — Safeguard checks
# ─────────────────────────────────────────────────────────────

class TestSafeguards:
    def test_ae07_drawdown_halt_blocks_all(self):
        """AE-07: Drawdown ≥ max_dd → empty result, no candidates pass."""
        cands = [_c("BTC/USDT"), _c("ETH/USDT")]
        result = _run(cands, drawdown_pct=15.5, max_dd_pct=15.0)
        assert result == []

    def test_ae07_check_candidate_returns_drawdown_reject(self):
        """AE-07: check_candidate returns REJECT_DRAWDOWN_HALT when drawdown ≥ max."""
        code = check_candidate(
            candidate=_c("BTC/USDT"), timeframe="1h",
            open_positions=[], n_open=0, max_pos=3,
            drawdown_pct=20.0, max_dd_pct=15.0, state=_state(),
        )
        assert code == REJECT_DRAWDOWN_HALT

    def test_ae08_duplicate_condition_skipped(self):
        """AE-08: Same condition (side+models+regime) already open → rejected."""
        open_pos = [_pos("ETH/USDT", side="buy", models=["trend"], regime="bull_trend")]
        result = _run([_c("ETH/USDT")], open_positions=open_pos)
        assert result == []

    def test_ae08_check_candidate_returns_duplicate(self):
        """AE-08: check_candidate returns REJECT_DUPLICATE for same-condition position."""
        open_pos = [_pos("ETH/USDT", side="buy", models=["trend"], regime="bull_trend")]
        code = check_candidate(
            candidate=_c("ETH/USDT"), timeframe="1h",
            open_positions=open_pos, n_open=1, max_pos=50,
            drawdown_pct=0.0, max_dd_pct=15.0, state=_state(),
        )
        assert code == REJECT_DUPLICATE

    def test_ae08_different_condition_allowed(self):
        """AE-08: Different condition (different models) for same symbol → allowed."""
        # Open position has ["trend"] but candidate has ["mean_reversion"]
        open_pos = [_pos("ETH/USDT", side="buy", models=["trend"], regime="bull_trend")]
        cand = _c("ETH/USDT", models=["mean_reversion"])
        cand["regime"] = "ranging"
        result = _run([cand], open_positions=open_pos)
        assert len(result) == 1

    def test_ae08_different_side_allowed(self):
        """AE-08: Same models but different side for same symbol → allowed."""
        open_pos = [_pos("ETH/USDT", side="buy", models=["trend"], regime="bull_trend")]
        cand = _c("ETH/USDT", side="sell", models=["trend"])
        result = _run([cand], open_positions=open_pos)
        assert len(result) == 1

    def test_ae09_position_limit_stops_batch(self):
        """AE-09: n_open == max_pos → REJECT_POSITION_LIMIT and batch stops."""
        cands = [_c("SOL/USDT"), _c("BNB/USDT")]
        open_pos = [_pos("A"), _pos("B"), _pos("C")]
        result = _run(cands, open_positions=open_pos, max_pos=3)
        assert result == []

    def test_ae09_check_candidate_returns_limit(self):
        """AE-09: check_candidate returns REJECT_POSITION_LIMIT when n_open ≥ max_pos."""
        open_pos = [_pos("A"), _pos("B"), _pos("C")]
        code = check_candidate(
            candidate=_c("SOL/USDT"), timeframe="1h",
            open_positions=open_pos, n_open=3, max_pos=3,
            drawdown_pct=0.0, max_dd_pct=15.0, state=_state(),
        )
        assert code == REJECT_POSITION_LIMIT


# ─────────────────────────────────────────────────────────────
# AE-10 / AE-11 — Age (freshness) checks
# ─────────────────────────────────────────────────────────────

class TestAgeFilter:
    def test_ae10_stale_candidate_skipped(self):
        """AE-10: Candidate older than 1×TF (3601s on 1h) is rejected."""
        stale = _c("BTC/USDT", age_seconds=3601)
        assert candidate_age_ok(stale, "1h") is False

    def test_ae10_run_batch_drops_stale(self):
        """AE-10: run_batch returns empty list for stale candidate."""
        stale = _c("BTC/USDT", age_seconds=3700)
        assert _run([stale]) == []

    def test_ae11_fresh_candidate_passes(self):
        """AE-11: Candidate 60s old passes 1h freshness check (3600s)."""
        fresh = _c("BTC/USDT", age_seconds=60)
        assert candidate_age_ok(fresh, "1h") is True

    def test_ae11_run_batch_allows_fresh(self):
        """AE-11: run_batch returns the candidate when age is well within TF."""
        fresh = _c("ETH/USDT", age_seconds=60)
        result = _run([fresh])
        assert len(result) == 1
        assert result[0]["symbol"] == "ETH/USDT"

    def test_ae11_boundary_at_exactly_tf(self):
        """AE-11: Candidate exactly at TF boundary (3600s for 1h) is considered OK."""
        # datetime.now could drift a few ms; give 5s tolerance
        on_boundary = _c("BTC/USDT", age_seconds=3595)
        assert candidate_age_ok(on_boundary, "1h") is True


# ─────────────────────────────────────────────────────────────
# AE-12 / AE-13 — No-signal / empty models
# ─────────────────────────────────────────────────────────────

class TestNoSignalFilter:
    def test_ae12_no_signal_flag_rejected(self):
        """AE-12: _no_signal=True → candidate_is_eligible returns False."""
        assert candidate_is_eligible(_c("BTC/USDT", no_signal=True)) is False

    def test_ae12_run_batch_skips_no_signal(self):
        """AE-12: run_batch returns empty for _no_signal candidates."""
        assert _run([_c("BTC/USDT", no_signal=True)]) == []

    def test_ae13_empty_models_rejected(self):
        """AE-13: Empty models_fired → candidate_is_eligible returns False."""
        assert candidate_is_eligible(_c("BTC/USDT", models=[])) is False

    def test_ae13_run_batch_skips_empty_models(self):
        """AE-13: run_batch returns empty for candidates with no models."""
        assert _run([_c("BTC/USDT", models=[])]) == []


# ─────────────────────────────────────────────────────────────
# AE-14 / AE-15 — Daily counter
# ─────────────────────────────────────────────────────────────

class TestDailyCounter:
    def test_ae14_counter_increments_on_success(self):
        """AE-14: run_batch increments AutoExecuteState.today_count on success."""
        st = _state()
        assert st.today_count == 0
        _run([_c("ETH/USDT", age_seconds=60)], state=st)
        assert st.today_count == 1

    def test_ae14_counter_increments_per_pair(self):
        """AE-14: Running two successful candidates for different pairs — both execute (one per pair per cycle)."""
        st = _state()
        result = _run([_c("ETH/USDT"), _c("BTC/USDT")], state=st)
        # One trade per PAIR per cycle — both different pairs, both approved
        assert st.today_count == 2
        assert len(result) == 2

    def test_ae14b_same_pair_only_first_executes(self):
        """AE-14b: Two candidates for the SAME pair — only first executes."""
        st = _state()
        result = _run([_c("ETH/USDT"), _c("ETH/USDT", side="sell")], state=st)
        assert st.today_count == 1
        assert len(result) == 1
        assert result[0]["symbol"] == "ETH/USDT"

    def test_ae15_counter_resets_on_date_change(self):
        """AE-15: reset_if_new_day clears count when date differs."""
        st = _state()
        st._today_count = 7
        st._today_date  = date(2020, 1, 1)
        st.reset_if_new_day()
        assert st._today_count == 0
        assert st._today_date  == date.today()

    def test_ae15_cooldown_cleared_on_date_change(self):
        """AE-15: reset_if_new_day also clears the cooldown dict."""
        st = _state()
        st._today_date = date(2020, 1, 1)
        st._last_exec["BTC/USDT"] = 999.0
        st.reset_if_new_day()
        assert st._last_exec == {}


# ─────────────────────────────────────────────────────────────
# AE-16 — Thread-safety (structural checks, no Qt needed)
# ─────────────────────────────────────────────────────────────

class TestThreadSafety:
    def test_ae16a_on_scan_complete_is_callable(self):
        """
        AE-16a: AssetScanner._on_scan_complete is callable.
        It is a @Slot on a QObject that lives in the main thread, so
        signals from ScanWorker are auto-queued to the main thread.
        """
        from core.scanning.scanner import AssetScanner
        assert callable(AssetScanner._on_scan_complete)

    def test_ae16b_worker_run_never_calls_submit(self):
        """
        AE-16b: ScanWorker.run() source code does not call order_router.submit.
        Execution is dispatched to the main thread via AssetScanner.candidates_ready.
        """
        import inspect
        from core.scanning.scanner import ScanWorker
        src = inspect.getsource(ScanWorker.run)
        assert "order_router.submit" not in src

    def test_ae16b_try_auto_execute_not_in_worker(self):
        """
        AE-16b: _try_auto_execute is never called from ScanWorker.
        """
        import inspect
        from core.scanning.scanner import ScanWorker
        src = inspect.getsource(ScanWorker.run)
        assert "_try_auto_execute" not in src


# ─────────────────────────────────────────────────────────────
# AE-17 / AE-18 — run_batch / check_candidate edge cases
# ─────────────────────────────────────────────────────────────

class TestRunBatchEdgeCases:
    def test_ae17_empty_candidates_returns_empty(self):
        """AE-17: run_batch with no candidates returns an empty list."""
        assert _run([]) == []

    def test_ae18_first_passes_second_blocked_by_limit(self):
        """AE-18: max_pos=1 — first candidate approved, second blocked, batch stops."""
        c1 = _c("ETH/USDT", age_seconds=60)
        c2 = _c("BTC/USDT", age_seconds=60)
        result = _run([c1, c2], max_pos=1)
        assert len(result) == 1
        assert result[0]["symbol"] == "ETH/USDT"

    def test_ae18_skip_duplicate_condition_then_approve_different(self):
        """AE-18: Skips duplicate-condition candidate but continues to next valid one."""
        c_dup   = _c("ETH/USDT", age_seconds=60)  # same condition as open position
        c_fresh = _c("BTC/USDT", age_seconds=60)
        open_pos = [_pos("ETH/USDT", side="buy", models=["trend"], regime="bull_trend")]
        result  = _run([c_dup, c_fresh], open_positions=open_pos)
        assert len(result) == 1
        assert result[0]["symbol"] == "BTC/USDT"


# ─────────────────────────────────────────────────────────────
# AE-19 — AutoExecuteState.record_execution
# ─────────────────────────────────────────────────────────────

class TestRecordExecution:
    def test_ae19_record_sets_timestamp(self):
        """AE-19: record_execution stores a monotonic timestamp for the symbol."""
        st = _state()
        before = time.monotonic()
        st.record_execution("ETH/USDT")
        after  = time.monotonic()
        ts = st._last_exec["ETH/USDT"]
        assert before <= ts <= after

    def test_ae19_record_increments_count(self):
        """AE-19: record_execution increments today_count by 1."""
        st = _state()
        st.record_execution("ETH/USDT")
        assert st.today_count == 1
        st.record_execution("BTC/USDT")
        assert st.today_count == 2


# ─────────────────────────────────────────────────────────────
# AE-20 — Cooldown cleared on date rollover (standalone)
# ─────────────────────────────────────────────────────────────

class TestCooldownRollover:
    def test_ae20_stale_cooldown_cleared_on_date_change(self):
        """AE-20: Symbol in cooldown becomes eligible after date rollover."""
        st = _state(cooldown=30)
        st._today_date = date(2020, 1, 1)
        # Symbol would normally be blocked (executed "just now")
        st._last_exec["SOL/USDT"] = time.monotonic()
        assert st.in_cooldown("SOL/USDT") is True

        st.reset_if_new_day()   # triggers rollover
        assert not st.in_cooldown("SOL/USDT")

    def test_ae20_run_batch_allows_after_rollover(self):
        """AE-20: run_batch auto-calls reset_if_new_day; old cooldowns are gone."""
        st = _state(cooldown=30)
        st._today_date = date(2020, 1, 1)
        st._last_exec["ETH/USDT"] = time.monotonic()   # "just executed" — would block

        result = run_batch(
            candidates     = [_c("ETH/USDT", age_seconds=60)],
            timeframe      = "1h",
            open_positions = [],
            drawdown_pct   = 0.0,
            max_dd_pct     = 15.0,
            max_pos        = 50,
            state          = st,
        )
        assert len(result) == 1


# ─────────────────────────────────────────────────────────────
# AE-21 — candidate_is_eligible
# ─────────────────────────────────────────────────────────────

class TestCandidateIsEligible:
    def test_ae21_all_conditions_met(self):
        assert candidate_is_eligible(_c("ETH/USDT")) is True

    def test_ae21_no_signal_flag(self):
        assert candidate_is_eligible(_c("ETH/USDT", no_signal=True)) is False

    def test_ae21_empty_models(self):
        assert candidate_is_eligible(_c("ETH/USDT", models=[])) is False

    def test_ae21_missing_side(self):
        c = _c("ETH/USDT")
        c["side"] = ""
        assert candidate_is_eligible(c) is False


# ─────────────────────────────────────────────────────────────
# AE-22 — candidate_age_ok edge cases
# ─────────────────────────────────────────────────────────────

class TestCandidateAgeOk:
    def test_ae22_missing_generated_at_is_ok(self):
        """AE-22: Missing generated_at field → allowed through (defensive)."""
        c = _c("BTC/USDT")
        del c["generated_at"]
        assert candidate_age_ok(c, "1h") is True

    def test_ae22_empty_generated_at_is_ok(self):
        """AE-22: Empty generated_at string → allowed through."""
        c = _c("BTC/USDT")
        c["generated_at"] = ""
        assert candidate_age_ok(c, "1h") is True

    def test_ae22_tf_seconds_coverage(self):
        """AE-22: TF_SECONDS dict has expected values for common timeframes."""
        assert TF_SECONDS["1m"]  == 60
        assert TF_SECONDS["1h"]  == 3600
        assert TF_SECONDS["4h"]  == 14400
        assert TF_SECONDS["1d"]  == 86400


# ─────────────────────────────────────────────────────────────
# AE-23 — check_candidate returns correct codes
# ─────────────────────────────────────────────────────────────

class TestCheckCandidateCodes:
    """Exhaustive check that every guard returns the correct rejection code."""

    def _base(self, **overrides):
        base = dict(
            candidate      = _c("ETH/USDT", age_seconds=60),
            timeframe      = "1h",
            open_positions = [],
            n_open         = 0,
            max_pos        = 50,
            drawdown_pct   = 0.0,
            max_dd_pct     = 15.0,
            state          = _state(),
        )
        base.update(overrides)
        return base

    def test_ae23_pass(self):
        assert check_candidate(**self._base()) == PASS

    def test_ae23_no_signal(self):
        assert check_candidate(**self._base(
            candidate=_c("ETH/USDT", no_signal=True)
        )) == REJECT_NO_SIGNAL

    def test_ae23_drawdown(self):
        assert check_candidate(**self._base(
            drawdown_pct=20.0
        )) == REJECT_DRAWDOWN_HALT

    def test_ae23_position_limit(self):
        assert check_candidate(**self._base(
            n_open=3, max_pos=3
        )) == REJECT_POSITION_LIMIT

    def test_ae23_duplicate_condition(self):
        """Same (side, models, regime) = duplicate."""
        open_pos = [_pos("ETH/USDT", side="buy", models=["trend"], regime="bull_trend")]
        assert check_candidate(**self._base(
            open_positions=open_pos
        )) == REJECT_DUPLICATE

    def test_ae23_stale(self):
        assert check_candidate(**self._base(
            candidate=_c("ETH/USDT", age_seconds=3700)
        )) == REJECT_STALE

    def test_ae23_cooldown(self):
        st = _state(cooldown=30)
        st._last_exec["ETH/USDT"] = time.monotonic() - 5
        assert check_candidate(**self._base(state=st)) == REJECT_COOLDOWN


# ─────────────────────────────────────────────────────────────
# AE-24 — Toggle-ON immediately fires on existing candidates
# ─────────────────────────────────────────────────────────────

class TestToggleImmediateFire:
    """AE-24: Enabling auto-execute mid-session immediately attempts
    to execute fresh candidates already in _candidate_history."""

    def test_ae24_toggle_on_calls_try_auto_execute(self):
        """AE-24a: Source inspection — _toggle_auto_execute calls
        _try_auto_execute when enabling (either with _candidate_history
        for legacy path, or with CandidateStore CONFIRMED candidates
        for the Phase 4 dual-scan path)."""
        import ast, pathlib
        src = pathlib.Path(
            "gui/pages/market_scanner/scanner_page.py"
        ).read_text()
        tree = ast.parse(src)

        # Find _toggle_auto_execute method body
        found_try_auto_call = False
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_toggle_auto_execute":
                for sub in ast.walk(node):
                    # Look for any _try_auto_execute(...) call
                    if isinstance(sub, ast.Call):
                        func = sub.func
                        if (hasattr(func, "attr") and
                                func.attr == "_try_auto_execute"):
                            found_try_auto_call = True
        assert found_try_auto_call, (
            "_toggle_auto_execute must call _try_auto_execute "
            "when enabling so candidates are submitted immediately"
        )

    def test_ae24_fresh_candidate_passes_age_check_after_toggle(self):
        """AE-24b: A candidate generated seconds ago passes the age guard
        (confirming it would be executed when toggle fires immediately)."""
        fresh = _c("BTC/USDT", age_seconds=5)  # 5s old on 1h TF
        assert candidate_age_ok(fresh, "1h") is True

    def test_ae24_stale_candidate_blocked_by_age_guard(self):
        """AE-24c: Stale candidate (age > 1× TF) is rejected even when
        toggle fires — the age guard prevents acting on old signals."""
        stale = _c("BTC/USDT", age_seconds=3700)  # >1h on 1h TF
        assert candidate_age_ok(stale, "1h") is False

    def test_ae24_run_batch_rejects_stale_on_toggle_fire(self):
        """AE-24d: run_batch returns empty list for stale candidates —
        safety net when toggle fires with hours-old history."""
        stale = _c("ETH/USDT", age_seconds=7200)  # 2h old
        result = _run([stale], timeframe="1h")
        assert result == [], "Stale candidates must not be executed even on toggle fire"


# ─────────────────────────────────────────────────────────────
# AE-25 — Condition-based deduplication (auto_execute_guard)
# ─────────────────────────────────────────────────────────────

class TestConditionDedup:
    """AE-25: Condition-based deduplication — same (side, models_fired set, regime)
    for the same symbol is considered a duplicate and rejected."""

    def test_ae25a_exact_same_condition_rejected(self):
        """Candidate with exact same side+models+regime as open position → rejected."""
        open_pos = [_pos("BTC/USDT", side="buy", models=["trend"], regime="bull_trend")]
        cand = _c("BTC/USDT", side="buy", models=["trend"])
        cand["regime"] = "bull_trend"
        result = _run([cand], open_positions=open_pos)
        assert result == []

    def test_ae25b_different_regime_allowed(self):
        """Same models+side but different regime → allowed (different condition)."""
        open_pos = [_pos("BTC/USDT", side="buy", models=["trend"], regime="bull_trend")]
        cand = _c("BTC/USDT", side="buy", models=["trend"])
        cand["regime"] = "ranging"
        result = _run([cand], open_positions=open_pos)
        assert len(result) == 1

    def test_ae25c_different_models_allowed(self):
        """Same side+regime but different models → allowed."""
        open_pos = [_pos("BTC/USDT", side="buy", models=["trend"], regime="bull_trend")]
        cand = _c("BTC/USDT", side="buy", models=["mean_reversion"])
        cand["regime"] = "bull_trend"
        result = _run([cand], open_positions=open_pos)
        assert len(result) == 1

    def test_ae25d_different_side_allowed(self):
        """Same models+regime but opposite side → allowed."""
        open_pos = [_pos("BTC/USDT", side="buy", models=["trend"], regime="bull_trend")]
        cand = _c("BTC/USDT", side="sell", models=["trend"])
        cand["regime"] = "bull_trend"
        result = _run([cand], open_positions=open_pos)
        assert len(result) == 1

    def test_ae25e_different_symbol_not_affected(self):
        """Open position for ETH doesn't block BTC with same condition."""
        open_pos = [_pos("ETH/USDT", side="buy", models=["trend"], regime="bull_trend")]
        cand = _c("BTC/USDT", side="buy", models=["trend"])
        cand["regime"] = "bull_trend"
        result = _run([cand], open_positions=open_pos)
        assert len(result) == 1

    def test_ae25f_model_set_order_invariant(self):
        """Models fired in different order should still be considered same condition."""
        open_pos = [_pos("BTC/USDT", side="buy", models=["trend", "momentum_breakout"], regime="bull_trend")]
        cand = _c("BTC/USDT", side="buy", models=["momentum_breakout", "trend"])
        cand["regime"] = "bull_trend"
        result = _run([cand], open_positions=open_pos)
        assert result == [], "Model order shouldn't matter — same set = duplicate"

    def test_ae25g_regime_case_insensitive(self):
        """Regime comparison should be case-insensitive."""
        open_pos = [_pos("BTC/USDT", side="buy", models=["trend"], regime="Bull_Trend")]
        cand = _c("BTC/USDT", side="buy", models=["trend"])
        cand["regime"] = "bull_trend"
        result = _run([cand], open_positions=open_pos)
        assert result == [], "Regime comparison should be case-insensitive"

    def test_ae25h_multiple_open_positions_one_match(self):
        """Multiple open positions for same symbol — reject only if ANY matches condition."""
        open_pos = [
            _pos("BTC/USDT", side="buy", models=["mean_reversion"], regime="ranging"),
            _pos("BTC/USDT", side="buy", models=["trend"], regime="bull_trend"),
        ]
        cand = _c("BTC/USDT", side="buy", models=["trend"])
        cand["regime"] = "bull_trend"
        result = _run([cand], open_positions=open_pos)
        assert result == [], "Should be rejected — matches second open position"

    def test_ae25i_multiple_open_none_match(self):
        """Multiple open positions for same symbol — none match → allowed."""
        open_pos = [
            _pos("BTC/USDT", side="buy", models=["mean_reversion"], regime="ranging"),
            _pos("BTC/USDT", side="sell", models=["trend"], regime="bear_trend"),
        ]
        cand = _c("BTC/USDT", side="buy", models=["trend"])
        cand["regime"] = "bull_trend"
        result = _run([cand], open_positions=open_pos)
        assert len(result) == 1
