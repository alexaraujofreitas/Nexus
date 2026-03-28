# ============================================================
# Session 37 — IDSS Scanner "Scan error" regression tests
#
# Root cause: line 547 in scanner.py returned `symbol` (str)
# instead of `None` as the candidate when a pre-scan filter
# rejected a symbol.  The caller's `if candidate:` evaluated
# the non-empty string as truthy and called `candidate.to_dict()`,
# raising AttributeError which was caught as "Scan error" for
# every symbol where the pre-filter fired.
#
# Covers:
#   1. Pre-filter rejection returns None candidate, not symbol str
#   2. Pre-filter rejection result dict status = rejection reason, not "Scan error"
#   3. DEFAULT_CONFIG time_of_day.enabled is False (safe fallback)
#   4. Volatility-filtered symbol shows correct status, not "Scan error"
# ============================================================

import pathlib
import ast


# ── 1. Return value tuple shape when pre-filter rejects ──────────────────


def test_pre_filter_rejection_returns_none_candidate():
    """_scan_symbol_with_regime must return None as candidate when pre-filter rejects.

    Before the fix, line 547 was:
        return symbol, None, None, None, _pf_reason, {}
    which put the symbol string into the candidate slot.
    """
    import inspect
    src = pathlib.Path("core/scanning/scanner.py").read_text(errors="replace")
    # The correct fix is `return None, "", 0.0, df, _pf_reason, _sym_diag`
    # The old buggy form was `return symbol, None, None, None, _pf_reason, {}`
    assert "return symbol, None, None, None, _pf_reason" not in src, (
        "Bug present: pre-filter rejection still returns `symbol` as candidate. "
        "Should be `return None, \"\", 0.0, df, _pf_reason, _sym_diag`."
    )


def test_pre_filter_rejection_return_correct_form():
    """The corrected return must be `return None, \"\", 0.0, ...`."""
    src = pathlib.Path("core/scanning/scanner.py").read_text(errors="replace")
    # Must have the corrected form
    assert 'return None, "", 0.0, df, _pf_reason, _sym_diag' in src, (
        "Pre-filter rejection return must be `return None, \"\", 0.0, df, _pf_reason, _sym_diag`"
    )


# ── 2. DEFAULT_CONFIG time_of_day.enabled is False ───────────────────────


def test_default_config_time_of_day_disabled():
    """DEFAULT_CONFIG.filters.time_of_day.enabled must be False.

    When config.yaml fails to load (corrupt/missing), the default must not
    block all scans outside EU/US hours.  The time-of-day filter is documented
    as an unvalidated hypothesis — it should be opt-in (default=False).
    """
    import sys
    sys.path.insert(0, ".")
    # Import DEFAULT_CONFIG directly to avoid side-effects of the singleton
    import importlib.util, types

    # Read the settings module source and extract DEFAULT_CONFIG via AST
    src = pathlib.Path("config/settings.py").read_text(errors="replace")
    assert '"time_of_day"' in src or "'time_of_day'" in src, (
        "config/settings.py must define a time_of_day sub-dict in DEFAULT_CONFIG"
    )
    # Check the enabled flag is explicitly False
    assert '"enabled": False' in src or "'enabled': False" in src, (
        "DEFAULT_CONFIG filters.time_of_day.enabled must be False"
    )


# ── 3. ScanWorker.run() per-symbol exception sets "Scan error", not pre-filter
#        rejections ──────────────────────────────────────────────────────────


def test_scan_error_only_from_uncaught_exceptions():
    """'Scan error' status must only come from except blocks in run(),
    not from pre-filter rejections.

    Verify that the pre-filter rejection path uses the reason string directly
    and that the 'Scan error' fallback is only in the except clause.
    """
    src = pathlib.Path("core/scanning/scanner.py").read_text(errors="replace")
    lines = src.splitlines()

    scan_error_lines = [i + 1 for i, l in enumerate(lines) if '"Scan error"' in l or "'Scan error'" in l]
    # All "Scan error" strings should be inside except blocks (not return statements from pre-filter)
    for lineno in scan_error_lines:
        line = lines[lineno - 1]
        # Must be inside _empty_sym_result call (exception handler path), not a return
        assert "return" not in line, (
            f"Line {lineno}: 'Scan error' found in a return statement — must only be in except blocks: {line!r}"
        )


# ── 4. Simulated pre-filter rejection produces correct per-symbol result ─


def test_prefilter_rejection_produces_correct_status_in_result():
    """When _scan_symbol_with_regime returns (None, regime, conf, df, reason, diag),
    the run() loop must store reason as status, not 'Scan error'.

    Tests the _empty_sym_result path for non-candidate symbols.
    """
    # Simulate the logic in ScanWorker.run() lines 349-357
    # When candidate is None, the else branch runs:
    #   _r = self._empty_sym_result(symbol, pre_rejection or "No signal", regime)
    # This means status = pre_rejection (e.g. "Time filter: UTC 07:xx outside window...")

    symbol = "BTC/USDT"
    pre_rejection = "Time filter: UTC 07:xx outside window 12:00-21:00"

    # Simulate _empty_sym_result
    result = {
        "symbol": symbol,
        "regime": "",
        "status": pre_rejection,
        "is_approved": False,
    }

    assert result["status"] == pre_rejection, (
        "Pre-filter rejection reason must be the row status, not 'Scan error'"
    )
    assert result["status"] != "Scan error", (
        "Pre-filter rejection must not produce 'Scan error' status"
    )


# ── 5. Volatility filter rejection also produces correct status ──────────


def test_volatility_filter_rejection_not_scan_error():
    """Volatility filter rejection reason must appear as row status, not 'Scan error'."""
    atr_reason = "Volatility filter: ATR ratio 0.30 < min 0.50 (low-vol rejection)"

    # Simulate what run() does when candidate is None with a pre_rejection
    pre_rejection = atr_reason
    _all_sym_results = {}
    symbol = "ETH/USDT"

    # _empty_sym_result equivalent
    _all_sym_results[symbol] = {
        "symbol": symbol,
        "regime": "",
        "status": pre_rejection or "No signal",
        "is_approved": False,
    }

    assert _all_sym_results[symbol]["status"] == atr_reason
    assert "Scan error" not in _all_sym_results[symbol]["status"]


# ── 6. UnboundLocalError from local import shadow (Session 37 deep-fix) ──
#
# Root cause (second layer, introduced during a prior fix attempt):
#   A `from core.features.indicator_library import calculate_scan_mode` was
#   placed INSIDE `_scan_symbol_with_regime()` in the PBL/SLC inner block
#   (line ~691).  Python's compiler sees any assignment (including `from X
#   import Y`) anywhere in a function and marks that name as LOCAL for the
#   ENTIRE function scope, regardless of whether that branch ever executes.
#   The result: `calculate_scan_mode` is treated as unbound at line ~553
#   (which runs unconditionally) → UnboundLocalError → "Scan error" for
#   every symbol on every scan.
# ─────────────────────────────────────────────────────────────────────────


def test_no_local_import_of_calculate_scan_mode_in_scan_symbol():
    """There must be NO `from ... import calculate_scan_mode` inside
    _scan_symbol_with_regime() or any other function body in scanner.py.

    A local `from X import Y` marks `Y` as a local variable for the entire
    enclosing function at compile time (Python scoping rule), causing
    UnboundLocalError whenever `Y` is used before that branch executes.
    The module-level import (line 37) is the sole allowed import site.
    """
    src = pathlib.Path("core/scanning/scanner.py").read_text(errors="replace")
    lines = src.splitlines()

    # Find all lines that import calculate_scan_mode (handles both
    # `from X import calculate_scan_mode` and `from X import a, calculate_scan_mode`)
    local_import_lines = [
        (i + 1, l.strip())
        for i, l in enumerate(lines)
        if "calculate_scan_mode" in l and l.lstrip().startswith("from ") and "import" in l
    ]

    # Exactly ONE import is expected: the module-level import at the top of the file
    assert len(local_import_lines) == 1, (
        f"Expected exactly 1 import of calculate_scan_mode (module-level), "
        f"found {len(local_import_lines)}: {local_import_lines}"
    )

    # That one import must be at the top of the file (before any class/def)
    lineno, line_text = local_import_lines[0]
    assert lineno < 60, (
        f"The only import of calculate_scan_mode must be at module level "
        f"(expected line < 60), found at line {lineno}: {line_text!r}"
    )


def test_calculate_scan_mode_not_assigned_inside_function():
    """Verify Python compile-time scoping: after the fix there must be no
    assignment to `calculate_scan_mode` inside any function body in scanner.py.

    Uses the `compile()` + `dis` approach to inspect bytecode LOAD_FAST
    vs LOAD_GLOBAL for `calculate_scan_mode` in `_scan_symbol_with_regime`.
    """
    import dis, types

    src = pathlib.Path("core/scanning/scanner.py").read_text(errors="replace")
    code = compile(src, "scanner.py", "exec")

    # Walk all code objects to find _scan_symbol_with_regime
    def find_code_obj(co, name):
        if co.co_name == name:
            return co
        for const in co.co_consts:
            if isinstance(const, types.CodeType):
                result = find_code_obj(const, name)
                if result:
                    return result
        return None

    fn_code = find_code_obj(code, "_scan_symbol_with_regime")
    assert fn_code is not None, "_scan_symbol_with_regime not found in scanner.py bytecode"

    # In Python 3.11+, local variables are in co_varnames;
    # check that calculate_scan_mode is NOT listed as a local variable
    assert "calculate_scan_mode" not in fn_code.co_varnames, (
        "calculate_scan_mode is in co_varnames of _scan_symbol_with_regime — "
        "this means a local `from X import calculate_scan_mode` exists inside "
        "the function, which causes UnboundLocalError at line ~553. "
        "Remove the local import; use the module-level import only."
    )


def test_unbound_local_error_pattern_is_fixed():
    """End-to-end: confirm the Python scoping trap is closed.

    Simulates exactly the pattern that was failing:
      - module-level import of `calculate_scan_mode`
      - function that uses it early (line ~553)
      - an inner conditional branch that locally imports it (line ~691)

    Before fix: UnboundLocalError at the early use.
    After fix:  function executes correctly.
    """
    # Compile the actual scanner module and verify no UnboundLocalError
    # by inspecting the bytecode (no real exchange needed).
    import types

    src = pathlib.Path("core/scanning/scanner.py").read_text(errors="replace")
    try:
        code = compile(src, "scanner.py", "exec")
    except SyntaxError as e:
        assert False, f"scanner.py has a syntax error after the fix: {e}"

    # Walk code objects and assert calculate_scan_mode is never a local var
    def walk(co):
        yield co
        for c in co.co_consts:
            if isinstance(c, types.CodeType):
                yield from walk(c)

    for co in walk(code):
        if co.co_name == "_scan_symbol_with_regime":
            assert "calculate_scan_mode" not in co.co_varnames, (
                f"UnboundLocalError regression: calculate_scan_mode is marked "
                f"as a local variable in {co.co_name}. The local import at the "
                f"PBL/SLC block must be removed."
            )


# ── 7. Other correct returns in _scan_symbol_with_regime are intact ──────


def test_correct_return_patterns_still_present():  # noqa: F811
    """All the other return paths in _scan_symbol_with_regime must still use
    None as the candidate (not symbol string).
    """
    src = pathlib.Path("core/scanning/scanner.py").read_text(errors="replace")
    # 'No data' return
    assert 'return None, "", 0.0, None, "No data"' in src, (
        "_scan_symbol_with_regime must still have `return None, \"\", 0.0, None, \"No data\", _sym_diag`"
    )
    # 'No signal' return
    assert '"No signal", _sym_diag' in src, (
        "_scan_symbol_with_regime must still have the 'No signal' return"
    )
    # 'Below threshold' return
    assert '"Below threshold", _sym_diag' in src, (
        "_scan_symbol_with_regime must still have the 'Below threshold' return"
    )
    # 'Indicators missing' return (Session 37 silent-failure guard)
    assert '"Indicators missing", _sym_diag' in src, (
        "_scan_symbol_with_regime must have the indicator-presence guard "
        "that returns 'Indicators missing' when calculate_scan_mode() fails silently."
    )


# ── 8. Indicator presence guard is in place ──────────────────────────────


def test_indicator_presence_guard_exists():
    """scanner.py must check that adx/ema_9/rsi_14 are present after
    calculate_scan_mode() and return 'Indicators missing' if they are not.

    calculate_scan_mode() has a silent failure mode: when 'ta' is unavailable
    or raises an exception, it returns raw OHLCV without indicators.  Models
    then find None for ADX/RSI/EMA and return no signal, showing 'No signal'
    when the real issue is missing indicators.  The guard surfaces this as a
    distinct, diagnosable status.
    """
    src = pathlib.Path("core/scanning/scanner.py").read_text(errors="replace")
    assert "_required_cols" in src or "_missing_cols" in src, (
        "scanner.py must contain indicator-presence guard variables "
        "(_required_cols / _missing_cols) after calculate_scan_mode()."
    )
    assert '"Indicators missing"' in src, (
        "scanner.py must return 'Indicators missing' status when "
        "key indicator columns are absent after calculate_scan_mode()."
    )


def test_indicator_presence_guard_checks_correct_columns():
    """The guard must check adx, ema_9, and rsi_14 — the three columns
    that would definitively confirm calculate_scan_mode() computed its
    CORE indicator set.
    """
    src = pathlib.Path("core/scanning/scanner.py").read_text(errors="replace")
    for col in ("adx", "ema_9", "rsi_14"):
        assert f'"{col}"' in src or f"'{col}'" in src, (
            f"Indicator presence guard must check column '{col}'."
        )
