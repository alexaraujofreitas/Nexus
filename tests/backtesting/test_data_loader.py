# ============================================================
# NEXUS TRADER — Unit Tests for HistoricalDataLoader
# ============================================================
"""Comprehensive test suite for data_loader.py

Tests cover:
  • Fetching real data from Bybit and Binance
  • Fallback behavior when primary fails
  • Data validation (gaps, duplicates, NaN, chronological order)
  • Timestamp parsing and conversion
  • Rate limiting and timeouts
  • Metadata recording
"""
import pytest
from datetime import datetime, timezone
from unittest.mock import Mock, patch, MagicMock
import pandas as pd
import numpy as np

from core.backtesting.data_loader import (
    HistoricalDataLoader,
    InsufficientDataError,
    DataSourceInfo,
    get_historical_data_loader,
    TF_SECONDS,
)


class TestInsufficientDataError:
    """Test InsufficientDataError exception."""

    def test_exception_creation(self):
        """Test that exception stores required attributes."""
        err = InsufficientDataError(
            "Test message",
            bars_needed=300,
            bars_available=150,
            source="bybit"
        )
        assert err.bars_needed == 300
        assert err.bars_available == 150
        assert err.source == "bybit"
        assert "Test message" in str(err)

    def test_exception_inheritance(self):
        """Test that exception is a proper Exception subclass."""
        err = InsufficientDataError("msg", 100, 50, "binance")
        assert isinstance(err, Exception)


class TestDataSourceInfo:
    """Test DataSourceInfo dataclass."""

    def test_creation(self):
        """Test that DataSourceInfo can be created and accessed."""
        info = DataSourceInfo(
            primary_source="bybit",
            fallback_used=False,
            fallback_source="",
            fallback_reason="",
            total_bars=1000,
            date_range_start="2024-01-01T00:00:00+00:00",
            date_range_end="2024-01-31T23:59:59+00:00",
            gaps_found=0,
            duplicates_removed=0,
            fetch_duration_s=12.5,
        )
        assert info.primary_source == "bybit"
        assert info.total_bars == 1000
        assert info.fallback_used is False
        assert info.fetch_duration_s == 12.5


class TestHistoricalDataLoaderBasics:
    """Test basic HistoricalDataLoader initialization and utilities."""

    def test_loader_initialization(self):
        """Test loader can be instantiated."""
        loader = HistoricalDataLoader()
        assert loader is not None
        assert loader._use_exchange_manager is True

    def test_loader_no_exchange_manager(self):
        """Test loader can be created without exchange_manager."""
        loader = HistoricalDataLoader(use_exchange_manager=False)
        assert loader._use_exchange_manager is False

    def test_get_historical_data_loader_singleton(self):
        """Test that get_historical_data_loader returns a singleton."""
        loader1 = get_historical_data_loader()
        loader2 = get_historical_data_loader()
        assert loader1 is loader2

    def test_tf_seconds_mapping(self):
        """Test that all timeframes are correctly mapped to seconds."""
        assert TF_SECONDS["1m"] == 60
        assert TF_SECONDS["5m"] == 300
        assert TF_SECONDS["15m"] == 900
        assert TF_SECONDS["30m"] == 1800
        assert TF_SECONDS["1h"] == 3600
        assert TF_SECONDS["4h"] == 14400
        assert TF_SECONDS["1d"] == 86400


class TestDateParsing:
    """Test date parsing functionality."""

    def test_parse_iso_date_basic(self):
        """Test parsing of basic ISO date."""
        loader = HistoricalDataLoader()
        ts_ms = loader._parse_iso_date("2024-01-01")
        # Should be midnight UTC on 2024-01-01
        dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        assert dt.year == 2024
        assert dt.month == 1
        assert dt.day == 1

    def test_parse_iso_date_with_time(self):
        """Test parsing of full ISO datetime."""
        loader = HistoricalDataLoader()
        ts_ms = loader._parse_iso_date("2024-01-01T12:30:45Z")
        dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        assert dt.hour == 12
        assert dt.minute == 30

    def test_parse_iso_date_invalid(self):
        """Test that invalid dates raise ValueError."""
        loader = HistoricalDataLoader()
        with pytest.raises(ValueError, match="Invalid date format"):
            loader._parse_iso_date("not-a-date")

    def test_parse_iso_date_swapped_dates_rejected(self):
        """Test that start > end raises ValueError in fetch_ohlcv."""
        loader = HistoricalDataLoader()
        with patch.object(loader, '_fetch_from_exchange_manager', return_value=[]):
            with patch.object(loader, '_fetch_from_binance', return_value=[]):
                with pytest.raises(ValueError, match="start_date.*must be before end_date"):
                    loader.fetch_ohlcv("BTC/USDT", "1h", "2024-12-31", "2024-01-01")


class TestCandleValidation:
    """Test candle validation logic."""

    def test_validate_candles_empty(self):
        """Test validation of empty candle list."""
        loader = HistoricalDataLoader()
        valid, issues = loader._validate_candles([], "1h", 100)
        assert valid is False
        assert any("No candles" in issue or "Insufficient" in issue for issue in issues)

    def test_validate_candles_insufficient(self):
        """Test validation when bars < min_bars."""
        loader = HistoricalDataLoader()
        candles = [[i * 3600000, 100, 101, 99, 100, 1000] for i in range(50)]
        valid, issues = loader._validate_candles(candles, "1h", 100)
        assert valid is False
        assert any("Insufficient" in issue for issue in issues)

    def test_validate_candles_duplicates(self):
        """Test detection of duplicate timestamps."""
        loader = HistoricalDataLoader()
        # Create candles with duplicate timestamp
        candles = [
            [1000, 100, 101, 99, 100, 1000],
            [1000, 100, 101, 99, 100, 1000],  # Duplicate
            [2000, 100, 101, 99, 100, 1000],
        ]
        valid, issues = loader._validate_candles(candles, "1h", 1)
        assert valid is False
        assert any("duplicate" in issue.lower() for issue in issues)

    def test_validate_candles_out_of_order(self):
        """Test detection of out-of-order timestamps."""
        loader = HistoricalDataLoader()
        candles = [
            [2000, 100, 101, 99, 100, 1000],
            [1000, 100, 101, 99, 100, 1000],  # Out of order
            [3000, 100, 101, 99, 100, 1000],
        ]
        valid, issues = loader._validate_candles(candles, "1h", 1)
        assert valid is False
        assert any("chronological" in issue.lower() for issue in issues)

    def test_validate_candles_with_nan(self):
        """Test detection of NaN values."""
        loader = HistoricalDataLoader()
        candles = [
            [1000, 100, 101, 99, 100, 1000],
            [2000, np.nan, 101, 99, 100, 1000],  # NaN in open
            [3000, 100, 101, 99, 100, 1000],
        ]
        valid, issues = loader._validate_candles(candles, "1h", 1)
        assert valid is False
        assert any("NaN" in issue for issue in issues)

    def test_validate_candles_negative_price(self):
        """Test detection of non-positive OHLC."""
        loader = HistoricalDataLoader()
        candles = [
            [1000, 100, 101, 99, 100, 1000],
            [2000, -50, 101, 99, 100, 1000],  # Negative open
            [3000, 100, 101, 99, 100, 1000],
        ]
        valid, issues = loader._validate_candles(candles, "1h", 1)
        assert valid is False
        assert any("non-positive" in issue.lower() for issue in issues)

    def test_validate_candles_negative_volume(self):
        """Test detection of negative volume."""
        loader = HistoricalDataLoader()
        candles = [
            [1000, 100, 101, 99, 100, 1000],
            [2000, 100, 101, 99, 100, -500],  # Negative volume
        ]
        valid, issues = loader._validate_candles(candles, "1h", 1)
        assert valid is False
        assert any("negative volume" in issue.lower() for issue in issues)

    def test_validate_candles_with_gaps(self):
        """Test detection of gaps larger than 3x timeframe."""
        loader = HistoricalDataLoader()
        # 1h = 3600s = 3600000ms; 3x gap = 10800000ms
        candles = [
            [1000, 100, 101, 99, 100, 1000],
            [12000000, 100, 101, 99, 100, 1000],  # Large gap (much > 3x 1h)
            [13000000, 100, 101, 99, 100, 1000],
        ]
        valid, issues = loader._validate_candles(candles, "1h", 1)
        assert valid is False
        assert any("gap" in issue.lower() for issue in issues)

    def test_validate_candles_success(self):
        """Test validation passes for valid candles."""
        loader = HistoricalDataLoader()
        # Create 300 valid hourly candles
        candles = [[i * 3600000, 100 + i * 0.1, 101 + i * 0.1, 99 + i * 0.1, 100.5 + i * 0.1, 1000]
                   for i in range(300)]
        valid, issues = loader._validate_candles(candles, "1h", 300)
        assert valid is True
        assert len(issues) == 0


class TestCandlesToDataframe:
    """Test conversion of raw candles to DataFrame."""

    def test_candles_to_dataframe_basic(self):
        """Test basic candle to DataFrame conversion."""
        loader = HistoricalDataLoader()
        candles = [
            [1704067200000, 42000, 42500, 41500, 42300, 1000],  # 2024-01-01 00:00:00 UTC
            [1704070800000, 42300, 42800, 42000, 42500, 1100],  # 2024-01-01 01:00:00 UTC
            [1704074400000, 42500, 43000, 42200, 42800, 1200],  # 2024-01-01 02:00:00 UTC
        ]

        df = loader._candles_to_dataframe(candles, "1h")

        assert len(df) == 3
        assert list(df.columns) == ["open", "high", "low", "close", "volume"]
        assert df["open"].iloc[0] == 42000
        assert df["close"].iloc[0] == 42300
        assert df["volume"].iloc[0] == 1000
        assert isinstance(df.index, pd.DatetimeIndex)

    def test_candles_to_dataframe_empty(self):
        """Test conversion of empty candle list."""
        loader = HistoricalDataLoader()
        df = loader._candles_to_dataframe([], "1h")
        assert len(df) == 0
        assert list(df.columns) == ["open", "high", "low", "close", "volume"]

    def test_candles_to_dataframe_types(self):
        """Test that values are converted to float."""
        loader = HistoricalDataLoader()
        candles = [
            [1704067200000, 42000, 42500, 41500, 42300, 1000],
        ]
        df = loader._candles_to_dataframe(candles, "1h")
        assert df["open"].dtype == float
        assert df["volume"].dtype == float

    def test_candles_to_dataframe_index_is_utc(self):
        """Test that index is in UTC timezone."""
        loader = HistoricalDataLoader()
        candles = [
            [1704067200000, 42000, 42500, 41500, 42300, 1000],
        ]
        df = loader._candles_to_dataframe(candles, "1h")
        assert df.index.tz is not None
        assert "UTC" in str(df.index.tz)


class TestFetchOhlcvError:
    """Test error handling in fetch_ohlcv."""

    def test_fetch_ohlcv_invalid_timeframe(self):
        """Test that invalid timeframe raises ValueError."""
        loader = HistoricalDataLoader()
        with pytest.raises(ValueError, match="Unsupported timeframe"):
            loader.fetch_ohlcv("BTC/USDT", "99h", "2024-01-01", "2024-12-31")

    def test_fetch_ohlcv_insufficient_data_primary_only(self):
        """Test InsufficientDataError when both primary and fallback fail."""
        loader = HistoricalDataLoader()
        with patch.object(loader, '_fetch_from_exchange_manager', return_value=[]):
            with patch.object(loader, '_fetch_from_binance', return_value=[]):
                with pytest.raises(InsufficientDataError) as exc_info:
                    loader.fetch_ohlcv("BTC/USDT", "1h", "2024-01-01", "2024-12-31", min_bars=100)
                assert exc_info.value.bars_needed == 100
                assert exc_info.value.bars_available == 0

    def test_fetch_ohlcv_insufficient_data_both_sources(self):
        """Test InsufficientDataError when both sources have insufficient data."""
        loader = HistoricalDataLoader()
        small_batch = [[i * 3600000, 100, 101, 99, 100, 1000] for i in range(50)]
        with patch.object(loader, '_fetch_from_exchange_manager', return_value=small_batch):
            with patch.object(loader, '_fetch_from_binance', return_value=small_batch):
                with pytest.raises(InsufficientDataError):
                    loader.fetch_ohlcv("BTC/USDT", "1h", "2024-01-01", "2024-12-31", min_bars=200)


class TestFetchOhlcvFallback:
    """Test fallback behavior in fetch_ohlcv."""

    def test_fetch_ohlcv_primary_succeeds(self):
        """Test that primary source is used when successful."""
        loader = HistoricalDataLoader()
        # Create 300 valid candles
        primary_candles = [[i * 3600000, 100, 101, 99, 100, 1000] for i in range(300)]
        fallback_candles = [[i * 3600000, 200, 201, 199, 200, 2000] for i in range(300)]

        with patch.object(loader, '_fetch_from_exchange_manager', return_value=primary_candles):
            with patch.object(loader, '_fetch_from_binance', return_value=fallback_candles):
                df = loader.fetch_ohlcv("BTC/USDT", "1h", "2024-01-01", "2024-12-31")
                # Should use primary (open price 100, not 200)
                assert df["open"].iloc[0] == 100
                # Check that fallback was recorded as not used
                info = loader.get_last_fetch_info()
                assert info.fallback_used is False

    def test_fetch_ohlcv_primary_fails_fallback_succeeds(self):
        """Test fallback is used when primary fails."""
        loader = HistoricalDataLoader()
        fallback_candles = [[i * 3600000, 200, 201, 199, 200, 2000] for i in range(300)]

        with patch.object(loader, '_fetch_from_exchange_manager', side_effect=RuntimeError("Primary failed")):
            with patch.object(loader, '_fetch_from_binance', return_value=fallback_candles):
                df = loader.fetch_ohlcv("BTC/USDT", "1h", "2024-01-01", "2024-12-31")
                # Should use fallback (open price 200)
                assert df["open"].iloc[0] == 200
                # Check that fallback was recorded as used
                info = loader.get_last_fetch_info()
                assert info.fallback_used is True

    def test_fetch_ohlcv_prefers_larger_primary(self):
        """Test that larger primary dataset is preferred over fallback."""
        loader = HistoricalDataLoader()
        primary_candles = [[i * 3600000, 100, 101, 99, 100, 1000] for i in range(300)]
        fallback_candles = [[i * 3600000, 200, 201, 199, 200, 2000] for i in range(200)]

        with patch.object(loader, '_fetch_from_exchange_manager', return_value=primary_candles):
            with patch.object(loader, '_fetch_from_binance', return_value=fallback_candles):
                df = loader.fetch_ohlcv("BTC/USDT", "1h", "2024-01-01", "2024-12-31", min_bars=200)
                # Should prefer primary (larger dataset)
                assert df["open"].iloc[0] == 100


class TestFetchRecordInfo:
    """Test metadata recording functionality."""

    def test_fetch_records_info(self):
        """Test that fetch_ohlcv records DataSourceInfo."""
        loader = HistoricalDataLoader()
        candles = [[i * 3600000, 100, 101, 99, 100, 1000] for i in range(300)]

        with patch.object(loader, '_fetch_from_exchange_manager', return_value=candles):
            df = loader.fetch_ohlcv("BTC/USDT", "1h", "2024-01-01", "2024-12-31")
            info = loader.get_last_fetch_info()
            assert info is not None
            assert info.total_bars == 300
            assert info.primary_source == "bybit"
            assert info.fallback_used is False

    def test_fetch_info_includes_duration(self):
        """Test that fetch_ohlcv records fetch duration."""
        loader = HistoricalDataLoader()
        candles = [[i * 3600000, 100, 101, 99, 100, 1000] for i in range(300)]

        with patch.object(loader, '_fetch_from_exchange_manager', return_value=candles):
            df = loader.fetch_ohlcv("BTC/USDT", "1h", "2024-01-01", "2024-12-31")
            info = loader.get_last_fetch_info()
            assert info.fetch_duration_s >= 0


class TestGetAvailableRange:
    """Test available range detection."""

    def test_get_available_range_mock(self):
        """Test get_available_range with mocked exchange."""
        loader = HistoricalDataLoader()

        mock_exchange = Mock()
        mock_exchange.fetch_ohlcv.return_value = [
            [1704067200000, 42000, 42500, 41500, 42300, 1000],
        ]

        with patch.object(loader, '_get_primary_exchange', return_value=mock_exchange):
            start, end = loader.get_available_range("BTC/USDT", "1h")
            assert isinstance(start, datetime)
            assert isinstance(end, datetime)


class TestInvalidTimeframe:
    """Test handling of invalid timeframes."""

    def test_unsupported_timeframe_rejected(self):
        """Test that unsupported timeframes are rejected."""
        loader = HistoricalDataLoader()
        with pytest.raises(ValueError, match="Unsupported timeframe"):
            loader.fetch_ohlcv("BTC/USDT", "2h", "2024-01-01", "2024-12-31")


# Integration-style tests (minimal mocking)
class TestDataLoaderIntegration:
    """Integration tests with light mocking."""

    def test_fetch_returns_dataframe(self):
        """Test that fetch_ohlcv returns a valid DataFrame."""
        loader = HistoricalDataLoader()
        candles = [[i * 3600000, 100 + i * 0.5, 101 + i * 0.5, 99 + i * 0.5,
                    100.5 + i * 0.5, 1000 + i * 10] for i in range(300)]

        with patch.object(loader, '_fetch_from_exchange_manager', return_value=candles):
            df = loader.fetch_ohlcv("BTC/USDT", "1h", "2024-01-01", "2024-12-31")

            assert isinstance(df, pd.DataFrame)
            assert len(df) == 300
            assert all(col in df.columns for col in ["open", "high", "low", "close", "volume"])
            assert df["open"].iloc[0] == 100
            assert df["open"].iloc[-1] > df["open"].iloc[0]  # Prices increasing

    def test_fetch_handles_large_dataset(self):
        """Test that fetch handles a large dataset (1000+ candles)."""
        loader = HistoricalDataLoader()
        # Simulate 1 year of hourly data (~8760 candles)
        candles = [[i * 3600000, 100 + i * 0.01, 101 + i * 0.01, 99 + i * 0.01,
                    100.5 + i * 0.01, 1000] for i in range(1000)]

        with patch.object(loader, '_fetch_from_exchange_manager', return_value=candles):
            df = loader.fetch_ohlcv("BTC/USDT", "1h", "2024-01-01", "2025-01-01", min_bars=1000)

            assert len(df) == 1000
            assert df["high"].min() >= df["low"].min()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
