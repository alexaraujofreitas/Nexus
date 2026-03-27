"""
tests/unit/test_signals.py — SignalGenerator tests (SG-001 to SG-012)

Testing strategy
----------------
SignalGenerator is tested by injecting mock sub-models so we can control
is_active_in_regime() and evaluate() return values without needing real
indicator DataFrames.  The orchestrator is also patched to avoid a live
import chain during tests.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from core.signals.signal_generator import SignalGenerator
from core.meta_decision.order_candidate import ModelSignal


# ── helpers ──────────────────────────────────────────────────────────────────

def make_signal(symbol="BTC/USDT", direction="long", strength=0.75,
                model_name="trend") -> ModelSignal:
    return ModelSignal(
        symbol      = symbol,
        model_name  = model_name,
        direction   = direction,
        strength    = strength,
        entry_price = 65_000.0,
        stop_loss   = 63_700.0,
        take_profit = 67_600.0,
        timeframe   = "1h",
        regime      = "TRENDING_UP",
        rationale   = "Test signal",
        atr_value   = 650.0,
    )


def make_mock_model(active: bool = True, signal: ModelSignal = None,
                    name: str = "mock_model") -> MagicMock:
    """Return a mock sub-model."""
    m = MagicMock()
    m.name = name
    m.is_active_in_regime.return_value = active
    m.evaluate.return_value = signal
    return m


def make_sg(*models, warmup_done: bool = True) -> SignalGenerator:
    """
    Return a SignalGenerator with the given mock models.
    If warmup_done=True (default), pre-complete the warmup so signals can fire.
    """
    sg = SignalGenerator(models=list(models))
    sg._rl_model = None        # disable RL for all unit tests
    if warmup_done:
        sg._warmup_complete = True
    return sg


def dummy_df(n: int = 50) -> pd.DataFrame:
    return pd.DataFrame({
        "close":  np.linspace(60_000, 65_000, n),
        "volume": np.ones(n) * 100.0,
    })


# Patch orchestrator for all tests so we don't need a live orchestrator
@pytest.fixture(autouse=True)
def patch_orchestrator(monkeypatch):
    """
    Silence the orchestrator fetch inside generate() so tests
    don't fail with import/initialization errors from the full stack.
    """
    mock_orch = MagicMock()
    mock_sig  = MagicMock()
    mock_sig.direction    = "neutral"
    mock_sig.meta_signal  = 0.0
    mock_orch.get_signal.return_value = mock_sig

    with patch("core.signals.signal_generator.get_orchestrator",
               return_value=mock_orch, create=True):
        yield


# ══════════════════════════════════════════════════════════════════════════════
#  SG-001 — Warm-up guard: suppress signals during warm-up period
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_sg001_warmup_suppresses_signals():
    """
    A brand-new SignalGenerator (warmup_complete=False) must return an empty
    list immediately, regardless of how many models are registered.
    """
    sig = make_signal()
    active_model = make_mock_model(active=True, signal=sig)
    sg = make_sg(active_model, warmup_done=False)

    result = sg.generate("BTC/USDT", dummy_df(), "TRENDING_UP", "1h")

    assert result == [], f"Expected [] during warmup, got {result!r}"
    active_model.evaluate.assert_not_called()


@pytest.mark.unit
def test_sg001_warmup_decrements_counter():
    """
    Each call during warm-up must decrement _warmup_bars_remaining by 1.
    """
    sg = make_sg(warmup_done=False)
    initial = sg._warmup_bars_remaining

    sg.generate("BTC/USDT", dummy_df(), "TRENDING_UP", "1h")

    assert sg._warmup_bars_remaining == initial - 1


# ══════════════════════════════════════════════════════════════════════════════
#  SG-002 — Warm-up completes exactly after N bars
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_sg002_warmup_completes_after_n_bars():
    """
    With warmup_bars=3, the generator must still suppress signals on the 3rd
    call (remaining goes to 0), and fire on the 4th.
    """
    sig = make_signal()
    active_model = make_mock_model(active=True, signal=sig)
    sg = make_sg(active_model, warmup_done=False)
    sg.reset_warmup(bars=3)

    df = dummy_df()

    # Calls 1–2: warm-up counter decrements (3→2→1), signals suppressed
    for _ in range(2):
        result = sg.generate("BTC/USDT", df, "TRENDING_UP", "1h")
        assert result == []

    # Call 3: remaining goes to 0 → warmup_complete set, signals fire on THIS call
    result = sg.generate("BTC/USDT", df, "TRENDING_UP", "1h")
    assert len(result) == 1


# ══════════════════════════════════════════════════════════════════════════════
#  SG-003 — Active model fires after warmup
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_sg003_active_model_signal_returned():
    """
    After warm-up, an active model that returns a ModelSignal must have
    that signal included in generate()'s output list.
    """
    sig = make_signal(symbol="BTC/USDT", direction="long")
    m = make_mock_model(active=True, signal=sig)
    sg = make_sg(m)

    result = sg.generate("BTC/USDT", dummy_df(), "TRENDING_UP", "1h")

    assert len(result) == 1
    assert result[0] is sig
    m.evaluate.assert_called_once()


@pytest.mark.unit
def test_sg003_correct_args_forwarded_to_model():
    """
    generate() must forward symbol, df, regime, and timeframe to each model's
    evaluate() call unchanged.
    """
    sig = make_signal()
    m = make_mock_model(active=True, signal=sig)
    sg = make_sg(m)

    df = dummy_df()
    sg.generate("ETH/USDT", df, "RANGING", "4h")

    m.evaluate.assert_called_once_with("ETH/USDT", df, "RANGING", "4h")


# ══════════════════════════════════════════════════════════════════════════════
#  SG-004 — Inactive models are skipped entirely
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_sg004_inactive_model_not_evaluated():
    """
    A model that returns False from is_active_in_regime() must NOT have
    evaluate() called — it should be completely skipped.
    """
    m = make_mock_model(active=False, signal=make_signal())
    sg = make_sg(m)

    result = sg.generate("BTC/USDT", dummy_df(), "RANGING", "1h")

    assert result == []
    m.evaluate.assert_not_called()


@pytest.mark.unit
def test_sg004_mixed_active_inactive_models():
    """
    Only active models should fire; inactive ones must not contribute signals.
    """
    sig1 = make_signal(model_name="model_a")
    sig2 = make_signal(model_name="model_b")
    active   = make_mock_model(active=True,  signal=sig1, name="model_a")
    inactive = make_mock_model(active=False, signal=sig2, name="model_b")
    sg = make_sg(active, inactive)

    result = sg.generate("BTC/USDT", dummy_df(), "TRENDING_UP", "1h")

    assert len(result) == 1
    assert result[0] is sig1
    inactive.evaluate.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
#  SG-005 — Model returning None does not add a signal
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_sg005_none_signal_excluded():
    """
    A model that is active but returns None from evaluate() must not add
    anything to the signals list.
    """
    m = make_mock_model(active=True, signal=None)
    sg = make_sg(m)

    result = sg.generate("BTC/USDT", dummy_df(), "TRENDING_UP", "1h")

    assert result == []


# ══════════════════════════════════════════════════════════════════════════════
#  SG-006 — Crashing model doesn't crash the generator
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_sg006_crashed_model_is_isolated():
    """
    If a model's evaluate() raises an exception, generate() must catch it,
    log the error, and continue with the remaining models — not raise itself.
    """
    bad_model  = make_mock_model(active=True, name="bad_model")
    bad_model.evaluate.side_effect = RuntimeError("deliberate test failure")

    good_sig   = make_signal(model_name="good_model")
    good_model = make_mock_model(active=True, signal=good_sig, name="good_model")

    sg = make_sg(bad_model, good_model)

    # Must not raise
    result = sg.generate("BTC/USDT", dummy_df(), "TRENDING_UP", "1h")

    # Good model still fires
    assert len(result) == 1
    assert result[0] is good_sig


# ══════════════════════════════════════════════════════════════════════════════
#  SG-007 — Default model list contains all 8 sub-models
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_sg007_default_models_count():
    """
    v1.2: 4 active sub-models (mean_reversion, vwap_reversion, liquidity_sweep,
    order_book archived). RL model is separate and optional.
    """
    sg = SignalGenerator()

    assert len(sg._models) >= 4, (
        f"Expected ≥ 4 active models (v1.2 config), got {len(sg._models)}"
    )


@pytest.mark.unit
def test_sg007_default_model_names():
    """All v1.2 active core sub-model names must be present in the default list."""
    sg = SignalGenerator()
    model_names = {m.name for m in sg._models}

    # v1.2 active models — mean_reversion, vwap_reversion, liquidity_sweep,
    # order_book are archived (PF < 1.0 in Study 4)
    expected = {
        "trend", "momentum_breakout",
        "funding_rate", "sentiment",
    }
    missing = expected - model_names
    assert not missing, f"Missing active sub-models: {missing}"


# ══════════════════════════════════════════════════════════════════════════════
#  SG-008 — Custom model registration and unregistration
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_sg008_register_custom_model():
    """After register_custom_model(), _custom_model must be the provided model."""
    sg = make_sg()
    custom = MagicMock()
    custom.name = "my_custom_model"

    sg.register_custom_model(custom)

    assert sg._custom_model is custom


@pytest.mark.unit
def test_sg008_unregister_clears_custom_model():
    """After unregister_custom_model(), _custom_model must be None."""
    sg = make_sg()
    sg._custom_model = MagicMock()

    sg.unregister_custom_model()

    assert sg._custom_model is None


# ══════════════════════════════════════════════════════════════════════════════
#  SG-009 — Custom model signals are included in output
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_sg009_custom_model_signals_appended():
    """
    When a custom model is registered, its generate() output must be appended
    to the final signals list.
    """
    custom_sig = make_signal(model_name="custom", direction="short")
    custom = MagicMock()
    custom.generate.return_value = [custom_sig]

    sg = make_sg()                  # no built-in models
    sg.register_custom_model(custom)

    result = sg.generate("BTC/USDT", dummy_df(), "TRENDING_UP", "1h")

    assert custom_sig in result
    custom.generate.assert_called_once()


@pytest.mark.unit
def test_sg009_custom_model_error_is_isolated():
    """
    If the custom model raises, generate() must log and continue — not raise.
    """
    normal_sig = make_signal(model_name="normal")
    normal_model = make_mock_model(active=True, signal=normal_sig)

    custom = MagicMock()
    custom.generate.side_effect = RuntimeError("custom model crash")

    sg = make_sg(normal_model)
    sg.register_custom_model(custom)

    # Must not raise
    result = sg.generate("BTC/USDT", dummy_df(), "TRENDING_UP", "1h")

    # The normal model's signal still appears
    assert normal_sig in result


# ══════════════════════════════════════════════════════════════════════════════
#  SG-010 — reset_warmup resets warm-up state
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_sg010_reset_warmup_re_enables_guard():
    """
    Calling reset_warmup() after warm-up is complete must re-engage the guard
    so the next generate() call returns [] again.
    """
    sig = make_signal()
    m = make_mock_model(active=True, signal=sig)
    sg = make_sg(m, warmup_done=True)

    # Confirm it fires before reset
    assert len(sg.generate("BTC/USDT", dummy_df(), "TRENDING_UP", "1h")) == 1

    sg.reset_warmup(bars=2)

    # Now suppressed again
    result = sg.generate("BTC/USDT", dummy_df(), "TRENDING_UP", "1h")
    assert result == []


@pytest.mark.unit
def test_sg010_reset_warmup_respects_custom_bars():
    """reset_warmup(bars=N) must set _warmup_bars_remaining to N."""
    sg = make_sg(warmup_done=True)
    sg.reset_warmup(bars=50)

    assert sg._warmup_bars_remaining == 50
    assert sg._warmup_complete is False


# ══════════════════════════════════════════════════════════════════════════════
#  SG-011 — Multiple models firing simultaneously
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_sg011_multiple_models_all_included():
    """
    When multiple models are active and all return signals, every signal must
    appear in generate()'s output list.
    """
    sigs = [make_signal(model_name=f"model_{i}") for i in range(3)]
    models = [make_mock_model(active=True, signal=s, name=f"model_{i}")
              for i, s in enumerate(sigs)]
    sg = make_sg(*models)

    result = sg.generate("BTC/USDT", dummy_df(), "TRENDING_UP", "1h")

    assert len(result) == 3
    for sig in sigs:
        assert sig in result


# ══════════════════════════════════════════════════════════════════════════════
#  SG-012 — generate() always returns a list, never None
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_sg012_returns_list_when_no_signals():
    """generate() must return an empty list [], not None, when nothing fires."""
    sg = make_sg()   # no models

    result = sg.generate("BTC/USDT", dummy_df(), "RANGING", "1h")

    assert result is not None
    assert isinstance(result, list)
    assert result == []


@pytest.mark.unit
def test_sg012_return_type_is_list_not_none_during_warmup():
    """generate() must return [] (not None) even during the warmup period."""
    sg = make_sg(warmup_done=False)

    result = sg.generate("BTC/USDT", dummy_df(), "TRENDING_UP", "1h")

    assert result is not None
    assert isinstance(result, list)
