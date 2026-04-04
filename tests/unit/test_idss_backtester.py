"""
tests/unit/test_idss_backtester.py — IDSSBacktester Realism Contracts (BT-001 to BT-007)

Audit findings addressed:
  BACK-02 — fee_pct default was 0.10 % (5× Bybit actual 0.02% maker).
             Fixed to 0.04 % (blended maker/taker).
  BACK-03 — Same-bar fill: signal generated from df.iloc[:i+1] (bar i's close
             already visible) and position opened at bar i's close on the same
             iteration — look-ahead bias.
             Fixed: signal on bar i stored as pending_candidate, entry executes
             at bar i+1's open.

Design: _run_pipeline and _passes_risk are patched so tests run in milliseconds
and don't require live IDSS pipeline components. _sizer is also patched to
return a deterministic trade_value.
"""
from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch, call

import numpy as np
import pandas as pd
import pytest

from core.backtesting.idss_backtester import IDSSBacktester, WalkForwardValidator


# ── shared fixtures ───────────────────────────────────────────────────────────

def _make_ohlcv(n: int = 20, base_price: float = 50_000.0) -> pd.DataFrame:
    """Minimal n-bar OHLCV with a UTC DatetimeIndex. Prices stay constant for
    deterministic SL/TP checks."""
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    p   = base_price
    return pd.DataFrame(
        {
            "open":   [p] * n,
            "high":   [p * 1.005] * n,
            "low":    [p * 0.995] * n,
            "close":  [p] * n,
            "volume": [100.0] * n,
        },
        index=idx,
    )


def _mock_candidate(
    entry_px:  float   = 50_000.0,
    side:      str     = "buy",
    sl_offset: float   = 500.0,
    tp_offset: float   = 1_500.0,
) -> MagicMock:
    """Minimal OrderCandidate mock sufficient for IDSSBacktester._passes_risk."""
    c = MagicMock()
    c.side              = side
    c.symbol            = "BTC/USDT"
    c.entry_price       = entry_px
    c.stop_loss_price   = (entry_px - sl_offset) if side == "buy" else (entry_px + sl_offset)
    c.take_profit_price = (entry_px + tp_offset) if side == "buy" else (entry_px - tp_offset)
    c.atr_value         = entry_px * 0.008
    c.score             = 0.65
    c.regime            = "trending_up" if side == "buy" else "trending_down"
    c.models_fired      = ["TrendModel"]
    return c


def _make_backtester(warmup: int = 5) -> IDSSBacktester:
    """
    Return an IDSSBacktester with all pipeline components patched out.
    Tests that need pipeline behavior inject their own side_effects.
    """
    with patch("core.backtesting.idss_backtester.IDSSBacktester.__init__", lambda self, *a, **kw: None):
        bt = IDSSBacktester.__new__(IDSSBacktester)

    # Inject minimal attributes that run() reads
    bt._threshold   = 0.45
    bt._warmup_bars = warmup

    mock_sizer = MagicMock()
    mock_sizer.calculate.return_value = 500.0   # $500 position every time
    bt._sizer    = mock_sizer
    bt._risk_gate = MagicMock()
    bt._regime_clf = MagicMock()
    bt._sig_gen    = MagicMock()
    bt._scorer     = MagicMock()

    return bt


# ══════════════════════════════════════════════════════════════════════════════
#  BT-001 — fee_pct default corrected to 0.04
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_fee_default_corrected():
    """
    BACK-02: IDSSBacktester.run() default fee_pct must be 0.04 %, not 0.10 %.
    0.10 % was 5× Bybit Demo's actual blended rate (~0.02% maker / 0.055% taker).
    """
    sig   = inspect.signature(IDSSBacktester.run)
    param = sig.parameters.get("fee_pct")
    assert param is not None, "fee_pct parameter not found in IDSSBacktester.run()"
    assert param.default == pytest.approx(0.04), (
        f"fee_pct default is {param.default!r} — expected 0.04 (Bybit blended rate). "
        "Former 0.10 % default inflated costs 5× (BACK-02)."
    )


# ══════════════════════════════════════════════════════════════════════════════
#  BT-002 — WalkForwardValidator fee_pct default also corrected
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_walk_forward_fee_default_corrected():
    """
    BACK-02 (companion): WalkForwardValidator.run() must also default to 0.04 %.
    Walk-forward passes fee_pct through to each IDSSBacktester.run() call.
    """
    sig   = inspect.signature(WalkForwardValidator.run)
    param = sig.parameters.get("fee_pct")
    assert param is not None, "fee_pct parameter not found in WalkForwardValidator.run()"
    assert param.default == pytest.approx(0.04), (
        f"WalkForwardValidator fee_pct default is {param.default!r} — expected 0.04."
    )


# ══════════════════════════════════════════════════════════════════════════════
#  BT-003 — No same-bar fill: entry_time must be bar AFTER the signal bar
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_no_same_bar_fill_entry_time():
    """
    BACK-03: Signal on bar i must produce an entry on bar i+1, NOT bar i.

    Mechanism: _run_pipeline returns a candidate exactly once, on bar `warmup`
    (the first eligible bar, i = warmup_bars).  The trade's entry_time must be
    the timestamp of bar warmup+1, not bar warmup.
    """
    warmup = 5
    df     = _make_ohlcv(n=warmup + 5)
    bt     = _make_backtester(warmup=warmup)

    signal_bar  = warmup          # first eligible bar (i = warmup_bars)
    signal_ts   = df.index[signal_bar]
    expected_ts = df.index[signal_bar + 1]   # entry must be on NEXT bar

    call_count = {"n": 0}

    def pipeline_side_effect(df_window, symbol, timeframe):
        call_count["n"] += 1
        # Return a candidate only on the very first eligible call
        if call_count["n"] == 1:
            return _mock_candidate()
        return None

    with (
        patch.object(bt, "_run_pipeline", side_effect=pipeline_side_effect),
        patch.object(bt, "_passes_risk",  return_value=True),
    ):
        result = bt.run(df, "BTC/USDT", "1h", fee_pct=0.04)

    trades = result["trades"]
    assert len(trades) >= 1, "Expected at least one trade to verify fill timing"

    trade      = trades[0]
    entry_time = trade["entry_time"]

    assert entry_time != str(signal_ts), (
        f"BACK-03 same-bar fill still present: entry_time={entry_time!r} "
        f"matches signal bar {signal_ts!r}.  Entry must be on the NEXT bar."
    )
    assert entry_time == str(expected_ts), (
        f"BACK-03: entry_time={entry_time!r} expected {str(expected_ts)!r} "
        f"(bar after signal bar {signal_ts!r})."
    )


# ══════════════════════════════════════════════════════════════════════════════
#  BT-004 — Entry price is at next bar's open (not signal bar's close)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_entry_price_at_next_bar_open():
    """
    BACK-03 (companion): Entry price must derive from bar i+1's open, not
    bar i's close.

    Construct a df where signal bar close != next bar open to confirm the
    correct price is used.
    """
    warmup = 5
    n      = warmup + 5
    idx    = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")

    closes = [50_000.0] * n
    opens  = [50_000.0] * n
    # Signal fires on bar `warmup`.  Bar `warmup+1` has a distinct open so we
    # can distinguish between "filled at bar warmup close" and "filled at bar
    # warmup+1 open".
    opens[warmup + 1] = 51_000.0   # deliberately different from 50_000 close

    df = pd.DataFrame(
        {
            "open":   opens,
            "high":   [p * 1.005 for p in closes],
            "low":    [p * 0.995 for p in closes],
            "close":  closes,
            "volume": [100.0] * n,
        },
        index=idx,
    )

    bt         = _make_backtester(warmup=warmup)
    call_count = {"n": 0}

    def pipeline_side_effect(df_window, symbol, timeframe):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _mock_candidate(entry_px=50_000.0)
        return None

    slippage_pct = 0.05
    spread_pct   = 0.05
    expected_open = 51_000.0
    # long fill: open * (1 + slip + spread/2)
    expected_fill = expected_open * (1.0 + slippage_pct / 100.0 + spread_pct / 200.0)

    with (
        patch.object(bt, "_run_pipeline", side_effect=pipeline_side_effect),
        patch.object(bt, "_passes_risk",  return_value=True),
    ):
        result = bt.run(df, "BTC/USDT", "1h",
                        fee_pct=0.04, slippage_pct=slippage_pct, spread_pct=spread_pct)

    trades = result["trades"]
    assert len(trades) >= 1, "Expected at least one trade for fill-price check"

    entry_price = trades[0]["entry_price"]
    assert entry_price == pytest.approx(expected_fill, rel=1e-6), (
        f"Entry price {entry_price:.4f} != expected {expected_fill:.4f} "
        f"(next-bar open {expected_open} with slip+spread). "
        "BACK-03: entry must use bar i+1's open, not bar i's close."
    )


# ══════════════════════════════════════════════════════════════════════════════
#  BT-005 — Lower fee improves profit factor on winning trades
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_lower_fee_improves_pnl():
    """
    Sanity: running the same backtest with fee=0.04 vs fee=0.10 must yield
    higher (less negative or more positive) PnL when there is at least one trade.

    Uses a df where the first trade is a guaranteed winner (price > SL, < TP
    on the signal bar, then hits TP on the next bar).
    """
    warmup = 5
    n      = warmup + 5
    idx    = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")

    prices = [50_000.0] * n
    # Bar warmup+2 (entry+1): force TP hit by setting high well above tp_offset=1500
    highs  = [p * 1.005 for p in prices]
    highs[warmup + 2] = 55_000.0   # well above TP at 51_500

    df = pd.DataFrame(
        {
            "open":   prices,
            "high":   highs,
            "low":    [p * 0.995 for p in prices],
            "close":  prices,
            "volume": [100.0] * n,
        },
        index=idx,
    )

    call_count = {"n": 0}

    def pipeline_se(df_window, symbol, timeframe):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _mock_candidate()
        call_count["n"] -= 0  # no-op, let counter accumulate
        return None

    results = {}
    for fee_val in (0.10, 0.04):
        call_count["n"] = 0
        bt = _make_backtester(warmup=warmup)
        with (
            patch.object(bt, "_run_pipeline", side_effect=pipeline_se),
            patch.object(bt, "_passes_risk",  return_value=True),
        ):
            results[fee_val] = bt.run(df, "BTC/USDT", "1h", fee_pct=fee_val)

    # Both runs must produce a trade
    for fv, res in results.items():
        assert len(res["trades"]) >= 1, f"No trades with fee_pct={fv}"

    pnl_low_fee  = results[0.04]["trades"][0]["pnl"]
    pnl_high_fee = results[0.10]["trades"][0]["pnl"]

    assert pnl_low_fee > pnl_high_fee, (
        f"Expected lower fee to yield better PnL: "
        f"fee=0.04 → {pnl_low_fee:.4f}, fee=0.10 → {pnl_high_fee:.4f}"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  BT-006 — Empty / None DataFrame returns empty result dict
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
@pytest.mark.parametrize("bad_df", [None, pd.DataFrame()])
def test_empty_df_returns_empty_result(bad_df):
    """
    run() must return a well-formed empty result dict for None or empty df.
    Must never raise.
    """
    bt = _make_backtester()
    result = bt.run(bad_df, "BTC/USDT", "1h")

    assert result["trades"]   == []
    assert result["candle_count"] == 0
    assert "metrics" in result
    assert result["metrics"]["total_trades"] == 0


# ══════════════════════════════════════════════════════════════════════════════
#  BT-007 — Open position at end of data is force-closed
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_force_close_at_end_of_data():
    """
    If a position is still open when the data ends (SL/TP never triggered),
    it must be closed at the final bar's close with exit_reason='end_of_data'.
    """
    warmup = 5
    # Flat price: SL and TP will never be hit (high/low stay within ±0.5%)
    df = _make_ohlcv(n=warmup + 4)
    bt = _make_backtester(warmup=warmup)

    call_count = {"n": 0}

    def pipeline_se(df_window, symbol, timeframe):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # SL far below, TP far above — will never trigger on flat price data
            return _mock_candidate(sl_offset=5_000.0, tp_offset=15_000.0)
        return None

    with (
        patch.object(bt, "_run_pipeline", side_effect=pipeline_se),
        patch.object(bt, "_passes_risk",  return_value=True),
    ):
        result = bt.run(df, "BTC/USDT", "1h", fee_pct=0.04)

    trades = result["trades"]
    assert len(trades) == 1, f"Expected 1 trade (force-closed at end), got {len(trades)}"
    assert trades[0]["exit_reason"] == "end_of_data", (
        f"Expected exit_reason='end_of_data', got {trades[0]['exit_reason']!r}"
    )
    # Force-close exit price must equal final bar's close (adjusted for slip/spread)
    last_close = df.iloc[-1]["close"]
    # long: exit_fill = last_close * (1 - slip - spread/2) ≈ last_close within 0.15%
    assert abs(trades[0]["exit_price"] - last_close) / last_close < 0.002, (
        "Force-close exit price deviates unexpectedly from final bar close"
    )
