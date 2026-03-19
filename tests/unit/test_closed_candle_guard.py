"""
Tests for core.scanning.closed_candle_guard — closed-candle enforcement.

Naming: CCG-xxx

Covers:
  - Forming candle detection and removal for all timeframes
  - Closed candle pass-through
  - Edge cases: empty data, unknown timeframe, exact boundary
  - now_ms override for deterministic testing
  - Logging verification
"""

import time

import pytest

from core.scanning.closed_candle_guard import (
    enforce_closed_candles,
    _TF_SECONDS,
)


# ── Helpers ────────────────────────────────────────────────────────────

def _make_ohlcv(count: int, tf_s: int, start_ms: int) -> list[list]:
    """Build a list of OHLCV rows with timestamps spaced by tf_s."""
    rows = []
    for i in range(count):
        ts = start_ms + i * tf_s * 1000
        rows.append([ts, 100.0, 101.0, 99.0, 100.5, 1000.0])
    return rows


def _bar_open_ms(hour: int, minute: int = 0) -> int:
    """UTC timestamp in ms for today at given hour:minute."""
    import datetime
    dt = datetime.datetime(2026, 3, 17, hour, minute, 0, tzinfo=datetime.timezone.utc)
    return int(dt.timestamp() * 1000)


class TestCCG_BasicBehavior:
    """CCG-001 through CCG-005: basic forming-candle detection."""

    def test_ccg001_forming_candle_is_dropped(self):
        """CCG-001: A bar whose period has not closed yet is removed."""
        tf = "1h"
        tf_ms = 3600 * 1000
        # 3 closed bars + 1 forming
        now_ms = _bar_open_ms(14, 30)  # 14:30 — the 14:00 candle has NOT closed
        ohlcv = _make_ohlcv(4, 3600, _bar_open_ms(11))
        # bars at 11:00, 12:00, 13:00, 14:00
        # 14:00 bar closes at 15:00, now is 14:30 → forming
        result, dropped = enforce_closed_candles(ohlcv, tf, now_ms=now_ms)
        assert dropped is True
        assert len(result) == 3
        assert result[-1][0] == _bar_open_ms(13)

    def test_ccg002_closed_candle_passes_through(self):
        """CCG-002: All bars are closed → no bar dropped."""
        now_ms = _bar_open_ms(15, 5)  # 15:05 — the 14:00 bar closed at 15:00
        ohlcv = _make_ohlcv(4, 3600, _bar_open_ms(11))
        result, dropped = enforce_closed_candles(ohlcv, "1h", now_ms=now_ms)
        assert dropped is False
        assert len(result) == 4

    def test_ccg003_exact_close_boundary(self):
        """CCG-003: Bar at exact close time is treated as closed."""
        # Bar opens at 14:00, closes at 15:00, now is exactly 15:00:00.000
        now_ms = _bar_open_ms(15, 0)
        ohlcv = _make_ohlcv(4, 3600, _bar_open_ms(11))
        result, dropped = enforce_closed_candles(ohlcv, "1h", now_ms=now_ms)
        assert dropped is False
        assert len(result) == 4

    def test_ccg004_one_ms_before_close_drops(self):
        """CCG-004: 1ms before close → forming candle detected."""
        close_ms = _bar_open_ms(15, 0)
        now_ms = close_ms - 1
        ohlcv = _make_ohlcv(4, 3600, _bar_open_ms(11))
        result, dropped = enforce_closed_candles(ohlcv, "1h", now_ms=now_ms)
        assert dropped is True
        assert len(result) == 3

    def test_ccg005_empty_data_returns_empty(self):
        """CCG-005: Empty input is passed through without error."""
        result, dropped = enforce_closed_candles([], "1h")
        assert dropped is False
        assert result == []


class TestCCG_MultipleTimeframes:
    """CCG-010 through CCG-016: verify enforcement across all timeframes."""

    @pytest.mark.parametrize("tf,tf_s", [
        ("1m", 60), ("5m", 300), ("15m", 900), ("30m", 1800),
        ("1h", 3600), ("4h", 14400), ("1d", 86400),
    ])
    def test_ccg010_forming_candle_detected_all_tfs(self, tf, tf_s):
        """CCG-010: Forming candle correctly detected for all standard TFs."""
        tf_ms = tf_s * 1000
        base_ms = 1710000000000  # arbitrary epoch
        ohlcv = _make_ohlcv(5, tf_s, base_ms)
        last_bar_open = ohlcv[-1][0]
        # now is halfway through the last bar
        now_ms = last_bar_open + tf_ms // 2
        result, dropped = enforce_closed_candles(ohlcv, tf, now_ms=now_ms)
        assert dropped is True
        assert len(result) == 4

    @pytest.mark.parametrize("tf,tf_s", [
        ("1m", 60), ("5m", 300), ("15m", 900), ("30m", 1800),
        ("1h", 3600), ("4h", 14400), ("1d", 86400),
    ])
    def test_ccg011_closed_candle_passes_all_tfs(self, tf, tf_s):
        """CCG-011: All-closed data passes through for all standard TFs."""
        tf_ms = tf_s * 1000
        base_ms = 1710000000000
        ohlcv = _make_ohlcv(5, tf_s, base_ms)
        last_bar_open = ohlcv[-1][0]
        # now is 1 second after the last bar closes
        now_ms = last_bar_open + tf_ms + 1000
        result, dropped = enforce_closed_candles(ohlcv, tf, now_ms=now_ms)
        assert dropped is False
        assert len(result) == 5


class TestCCG_EdgeCases:
    """CCG-020 through CCG-025: edge cases and error handling."""

    def test_ccg020_unknown_timeframe_passes_through(self):
        """CCG-020: Unknown timeframe logs warning and passes data through."""
        ohlcv = _make_ohlcv(3, 3600, 1710000000000)
        result, dropped = enforce_closed_candles(ohlcv, "2w", now_ms=1710000000000)
        assert dropped is False
        assert len(result) == 3

    def test_ccg021_single_bar_forming_dropped(self):
        """CCG-021: Single forming bar → returns empty list."""
        now_ms = _bar_open_ms(14, 30)
        ohlcv = [[_bar_open_ms(14), 100.0, 101.0, 99.0, 100.5, 1000.0]]
        result, dropped = enforce_closed_candles(ohlcv, "1h", now_ms=now_ms)
        assert dropped is True
        assert len(result) == 0

    def test_ccg022_single_bar_closed_passes(self):
        """CCG-022: Single closed bar → passes through."""
        now_ms = _bar_open_ms(15, 5)
        ohlcv = [[_bar_open_ms(14), 100.0, 101.0, 99.0, 100.5, 1000.0]]
        result, dropped = enforce_closed_candles(ohlcv, "1h", now_ms=now_ms)
        assert dropped is False
        assert len(result) == 1

    def test_ccg023_no_now_ms_uses_system_time(self):
        """CCG-023: When now_ms is None, system time is used."""
        # Build bars well in the past — all should be closed
        base_ms = int(time.time() * 1000) - 10 * 3600 * 1000  # 10 hours ago
        ohlcv = _make_ohlcv(5, 3600, base_ms)
        result, dropped = enforce_closed_candles(ohlcv, "1h")
        # Unless system time is extremely close to a bar boundary (near-impossible),
        # these should all be closed
        assert len(result) >= 4  # at least 4 closed

    def test_ccg024_15m_forming_at_minute_7(self):
        """CCG-024: 15m bar forming at 7 minutes in → 47% formed, dropped."""
        bar_open_ms = _bar_open_ms(14, 0)  # 14:00 bar
        now_ms = _bar_open_ms(14, 7)  # 14:07
        ohlcv = _make_ohlcv(10, 900, bar_open_ms - 9 * 900 * 1000)
        # last bar opens at 14:00, closes at 14:15, now is 14:07 → forming
        result, dropped = enforce_closed_candles(ohlcv, "15m", now_ms=now_ms)
        assert dropped is True
        assert len(result) == 9

    def test_ccg025_data_content_preserved(self):
        """CCG-025: The guard only removes bars, never modifies OHLCV values."""
        ohlcv = [
            [_bar_open_ms(12), 50000.0, 50500.0, 49800.0, 50200.0, 123.456],
            [_bar_open_ms(13), 50200.0, 50700.0, 50100.0, 50600.0, 234.567],
            [_bar_open_ms(14), 50600.0, 50900.0, 50400.0, 50800.0, 345.678],
        ]
        now_ms = _bar_open_ms(14, 30)  # 14:00 bar forming
        result, dropped = enforce_closed_candles(ohlcv, "1h", now_ms=now_ms)
        assert dropped is True
        assert len(result) == 2
        # Verify values are untouched
        assert result[0] == [_bar_open_ms(12), 50000.0, 50500.0, 49800.0, 50200.0, 123.456]
        assert result[1] == [_bar_open_ms(13), 50200.0, 50700.0, 50100.0, 50600.0, 234.567]


class TestCCG_LogSymbol:
    """CCG-030: log_symbol parameter is used for tracing."""

    def test_ccg030_log_symbol_in_output(self, caplog):
        """CCG-030: log_symbol appears in log messages."""
        import logging
        with caplog.at_level(logging.INFO, logger="core.scanning.closed_candle_guard"):
            now_ms = _bar_open_ms(14, 30)
            ohlcv = _make_ohlcv(3, 3600, _bar_open_ms(12))
            enforce_closed_candles(ohlcv, "1h", now_ms=now_ms, log_symbol="BTC/USDT")
        assert "BTC/USDT" in caplog.text
        assert "dropped forming candle" in caplog.text


class TestCCG_TFSecondsRegistry:
    """CCG-040: Verify the TF_SECONDS mapping is complete."""

    def test_ccg040_all_standard_tfs_registered(self):
        """CCG-040: All standard CCXT timeframes are in _TF_SECONDS."""
        expected = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d", "1w"}
        assert expected.issubset(set(_TF_SECONDS.keys()))

    def test_ccg041_values_are_positive_ints(self):
        """CCG-041: All TF durations are positive integers."""
        for tf, seconds in _TF_SECONDS.items():
            assert isinstance(seconds, int), f"{tf} value is not int"
            assert seconds > 0, f"{tf} value is not positive"
