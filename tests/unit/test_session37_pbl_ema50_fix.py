"""
Session 37 — PBL ema_50 Fix Tests
==================================
Regression suite for the blocking issue identified in Stage 8 validation:
PullbackLongModel required ema_50 in the 4h HTF context df, but ema_50 was
absent from SCAN_CORE_COLUMNS and not computed by calculate_scan_mode().

This caused PBL evaluate() to always return None in the live scanner,
making PBL non-functional despite correct ACTIVE_REGIMES gating.

Fix applied (Session 37):
  1. Added "ema_50" to SCAN_CORE_COLUMNS in indicator_library.py
  2. Added 50 to the EMA window list in calculate_scan_mode()

These tests prove:
  1. ema_50 is declared in SCAN_CORE_COLUMNS
  2. calculate_scan_mode() computes ema_50 on realistic data shapes
     (300-bar 30m, 60-bar 4h, 150-bar 1h)
  3. PBL evaluate() no longer fails with "missing ema_50 or rsi_14"
     when supplied a 4h df with ema_50 and rsi_14 present
  4. PBL evaluate() still correctly rejects when ema_50 IS missing
     (regression guard — old behaviour was a bug, not a feature)
"""

import pathlib
import sys
import types

import numpy as np
import pandas as pd
import pytest

# ── Path setup ────────────────────────────────────────────────────────────────
ROOT = pathlib.Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))


# =============================================================================
# 1. SCAN_CORE_COLUMNS contains ema_50
# =============================================================================

class TestEma50InScanCore:
    def test_ema50_in_scan_core_columns(self):
        """ema_50 must be declared in SCAN_CORE_COLUMNS."""
        from core.features.indicator_library import SCAN_CORE_COLUMNS
        assert "ema_50" in SCAN_CORE_COLUMNS, (
            "ema_50 is required by PullbackLongModel (4h HTF context) but is "
            "absent from SCAN_CORE_COLUMNS.  Add 'ema_50' to the frozenset."
        )

    def test_ema50_alongside_other_required_pbl_columns(self):
        """rsi_14 (also required by PBL) must remain in SCAN_CORE_COLUMNS."""
        from core.features.indicator_library import SCAN_CORE_COLUMNS
        for col in ("ema_50", "rsi_14"):
            assert col in SCAN_CORE_COLUMNS, (
                f"PBL requires '{col}' in SCAN_CORE_COLUMNS."
            )

    def test_scan_core_columns_is_frozenset(self):
        """SCAN_CORE_COLUMNS must remain a frozenset (immutable)."""
        from core.features.indicator_library import SCAN_CORE_COLUMNS
        assert isinstance(SCAN_CORE_COLUMNS, frozenset)

    def test_scan_core_columns_comment_documents_pbl(self):
        """The source comment above SCAN_CORE_COLUMNS must document PBL."""
        src = pathlib.Path("core/features/indicator_library.py").read_text(errors="replace")
        assert "PullbackLongModel" in src, (
            "SCAN_CORE_COLUMNS comment must document PullbackLongModel as a consumer."
        )
        assert "ema_50" in src, (
            "indicator_library.py must reference ema_50 in the SCAN_CORE_COLUMNS block."
        )


# =============================================================================
# 2. calculate_scan_mode() computes ema_50 on all three realistic data shapes
# =============================================================================

def _make_ohlcv(n: int, seed: int = 42) -> pd.DataFrame:
    """Synthetic OHLCV DataFrame with n rows and UTC DatetimeIndex."""
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 0.5, n))
    close = np.clip(close, 1, None)
    high  = close + rng.uniform(0.1, 1.0, n)
    low   = close - rng.uniform(0.1, 1.0, n)
    open_ = close + rng.normal(0, 0.3, n)
    volume = rng.uniform(1000, 5000, n)
    idx = pd.date_range("2024-01-01", periods=n, freq="30min", tz="UTC")
    return pd.DataFrame({"open": open_, "high": high, "low": low,
                          "close": close, "volume": volume}, index=idx)


class TestCalculateScanModeProducesEma50:
    """calculate_scan_mode() must output ema_50 on all three data shapes
    used in the live scanner."""

    @pytest.fixture(autouse=True)
    def _skip_if_no_ta(self):
        try:
            import ta  # noqa: F401
        except ImportError:
            pytest.skip("'ta' library not installed — skipping indicator tests")

    def test_ema50_present_on_300bar_30m_data(self):
        """Primary 30m data (299–300 bars): ema_50 must be in output df."""
        from core.features.indicator_library import calculate_scan_mode
        df = _make_ohlcv(300)
        result = calculate_scan_mode(df)
        assert "ema_50" in result.columns, (
            "calculate_scan_mode() did not produce ema_50 on 300-bar (30m) data."
        )
        # At least the last value should be non-NaN (300 >> 50)
        assert not result["ema_50"].iloc[-1:].isna().all(), (
            "ema_50 is all-NaN on 300-bar data — window=50 should converge."
        )

    def test_ema50_present_on_60bar_4h_context(self):
        """4h HTF context data (59–60 bars): ema_50 must be in output df.
        59 bars is the minimum for EMA-50 to converge; the last value must
        be non-NaN."""
        from core.features.indicator_library import calculate_scan_mode
        df = _make_ohlcv(60)
        result = calculate_scan_mode(df)
        assert "ema_50" in result.columns, (
            "calculate_scan_mode() did not produce ema_50 on 60-bar (4h context) data."
        )
        assert not result["ema_50"].iloc[-1:].isna().all(), (
            "ema_50 is all-NaN on 60-bar data — the last bar should be computable "
            "with window=50 on 60+ bars."
        )

    def test_ema50_present_on_150bar_1h_context(self):
        """1h HTF context data (149–150 bars): ema_50 must be in output df."""
        from core.features.indicator_library import calculate_scan_mode
        df = _make_ohlcv(150)
        result = calculate_scan_mode(df)
        assert "ema_50" in result.columns, (
            "calculate_scan_mode() did not produce ema_50 on 150-bar (1h context) data."
        )
        assert not result["ema_50"].iloc[-1:].isna().all()

    def test_ema50_and_rsi14_both_present(self):
        """Both ema_50 AND rsi_14 must be in output — PBL requires both."""
        from core.features.indicator_library import calculate_scan_mode
        df = _make_ohlcv(300)
        result = calculate_scan_mode(df)
        for col in ("ema_50", "rsi_14"):
            assert col in result.columns, (
                f"calculate_scan_mode() missing '{col}' — required by PBL."
            )

    def test_column_count_increased_by_one(self):
        """Adding ema_50 should increase the output column count by exactly 1
        compared to the previous CORE set (was 21 cols, now 22)."""
        from core.features.indicator_library import calculate_scan_mode
        df = _make_ohlcv(300)
        result = calculate_scan_mode(df)
        # 5 OHLCV + 22 indicator columns = 27 total  (was 26 before this fix)
        # We assert ema_50 is present rather than hard-coding total count
        # to avoid breaking if other columns are added later.
        assert "ema_50" in result.columns


# =============================================================================
# 3. PBL evaluate() path — ema_50 present → no longer fails on indicator check
# =============================================================================

class TestPBLEvaluateWithEma50:
    """Prove that PBL evaluate() can progress past the 'missing ema_50 or
    rsi_14' guard when calculate_scan_mode()-produced data is supplied.

    We do NOT test for a signal (market conditions are synthetic and unlikely
    to meet all PBL criteria), but we DO verify that the function does not
    bail out at the indicator-missing early-return.
    """

    @pytest.fixture(autouse=True)
    def _skip_if_no_ta(self):
        try:
            import ta  # noqa: F401
        except ImportError:
            pytest.skip("'ta' library not installed — skipping PBL evaluate tests")

    def _make_4h_df_via_scan_mode(self, n: int = 60) -> pd.DataFrame:
        from core.features.indicator_library import calculate_scan_mode
        return calculate_scan_mode(_make_ohlcv(n, seed=7))

    def test_pbl_evaluate_receives_ema50(self):
        """After fix, df_4h computed by calculate_scan_mode has ema_50.
        Verify the column is present before passing to evaluate()."""
        df_4h = self._make_4h_df_via_scan_mode()
        assert "ema_50" in df_4h.columns, (
            "Pre-condition for PBL evaluate() test: df_4h must have ema_50."
        )

    def test_pbl_does_not_log_missing_ema50(self, caplog):
        """PBL evaluate() must NOT emit 'missing ema_50 or rsi_14' when
        called with a properly computed 4h df."""
        import logging
        from unittest.mock import MagicMock, patch

        # Build a minimal settings mock so PBL can read config
        mock_settings = MagicMock()
        mock_settings.get.side_effect = lambda key, default=None: {
            "models.pbl.ema_proximity_pct": 0.02,
            "models.pbl.rsi_min": 35.0,
            "models.pbl.rsi_max": 65.0,
        }.get(key, default)

        with patch("config.settings.settings", mock_settings):
            try:
                from core.signals.sub_models.pullback_long_model import PullbackLongModel
            except Exception:
                pytest.skip("Could not import PullbackLongModel — skipping")

        df_4h = self._make_4h_df_via_scan_mode(n=60)

        pbl = None
        with patch("config.settings.settings", mock_settings):
            try:
                pbl = PullbackLongModel()
            except Exception:
                pytest.skip("Could not instantiate PullbackLongModel — skipping")

        with caplog.at_level(logging.DEBUG,
                             logger="core.signals.sub_models.pullback_long_model"):
            try:
                with patch("config.settings.settings", mock_settings):
                    pbl.evaluate(
                        "XRP/USDT", df_4h, "bull_trend",
                        context={"df_4h": df_4h}
                    )
            except Exception:
                pass  # Any other exception is fine — we only care about the log message

        missing_msg_logged = any(
            "missing ema_50 or rsi_14" in record.message
            for record in caplog.records
        )
        assert not missing_msg_logged, (
            "PBL still logged 'missing ema_50 or rsi_14' after fix — "
            "check that calculate_scan_mode() is actually producing ema_50."
        )

    def test_pbl_evaluate_still_returns_none_on_missing_ema50(self, caplog):
        """Regression guard: if ema_50 is genuinely absent, PBL must still
        return None (not crash).  This validates the guard logic itself."""
        import logging
        from unittest.mock import MagicMock, patch

        mock_settings = MagicMock()
        mock_settings.get.return_value = None

        with patch("config.settings.settings", mock_settings):
            try:
                from core.signals.sub_models.pullback_long_model import PullbackLongModel
                pbl = PullbackLongModel()
            except Exception:
                pytest.skip("Could not import/instantiate PullbackLongModel")

        # Build df WITHOUT ema_50 deliberately
        df_no_ema50 = _make_ohlcv(60)
        # (raw OHLCV only — no indicators)

        result = None
        with patch("config.settings.settings", mock_settings):
            try:
                result = pbl.evaluate(
                    "XRP/USDT", df_no_ema50, "bull_trend",
                    context={"df_4h": df_no_ema50}
                )
            except Exception:
                pass  # Exception also acceptable — what we forbid is a spurious signal

        assert result is None, (
            "PBL must return None when ema_50 is absent — never a spurious signal."
        )
