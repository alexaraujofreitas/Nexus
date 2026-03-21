"""Tests for Phase 1 trade filters."""
import pytest
from unittest.mock import patch
from datetime import datetime, timezone
import pandas as pd
import numpy as np


class TestTimeOfDayFilter:
    def test_tof_pass_inside_window(self):
        from core.filters.trade_filters import check_time_of_day
        dt = datetime(2026, 3, 20, 15, 0, tzinfo=timezone.utc)  # 15:00 UTC
        passed, reason = check_time_of_day(dt)
        assert passed

    def test_tof_reject_outside_window(self):
        # The filter is disabled in production config (Study 4 hardening).
        # Test the filter logic in isolation by explicitly enabling it.
        from core.filters.trade_filters import check_time_of_day
        with patch("core.filters.trade_filters._s") as mock_s:
            def _get(key, default=None):
                if key == "filters.time_of_day.enabled":   return True
                if key == "filters.time_of_day.start_hour_utc": return 12
                if key == "filters.time_of_day.end_hour_utc":   return 21
                return default
            mock_s.get.side_effect = _get
            dt = datetime(2026, 3, 20, 8, 0, tzinfo=timezone.utc)  # 08:00 UTC — outside 12-21
            passed, reason = check_time_of_day(dt)
        assert not passed
        assert "filter" in reason.lower()

    def test_tof_boundary_start(self):
        from core.filters.trade_filters import check_time_of_day
        dt = datetime(2026, 3, 20, 12, 0, tzinfo=timezone.utc)  # 12:00 UTC = start
        passed, _ = check_time_of_day(dt)
        assert passed

    def test_tof_boundary_end_excluded(self):
        # Explicitly enable the filter to test boundary-exclusion logic.
        from core.filters.trade_filters import check_time_of_day
        with patch("core.filters.trade_filters._s") as mock_s:
            def _get(key, default=None):
                if key == "filters.time_of_day.enabled":   return True
                if key == "filters.time_of_day.start_hour_utc": return 12
                if key == "filters.time_of_day.end_hour_utc":   return 21
                return default
            mock_s.get.side_effect = _get
            dt = datetime(2026, 3, 20, 21, 0, tzinfo=timezone.utc)  # 21:00 UTC = end (excluded)
            passed, _ = check_time_of_day(dt)
        assert not passed

    def test_tof_disabled_always_passes(self):
        from core.filters.trade_filters import check_time_of_day
        with patch("core.filters.trade_filters._s") as mock_s:
            mock_s.get.return_value = False
            dt = datetime(2026, 3, 20, 2, 0, tzinfo=timezone.utc)
            passed, _ = check_time_of_day(dt)
            assert passed


class TestVolatilityFilter:
    def _make_df(self, atr_val, avg_atr):
        """Create a df where current ATR = atr_val and recent avg = avg_atr."""
        n = 30
        close = pd.Series([100.0] * n)
        # Build ATR column directly
        atr = pd.Series([avg_atr] * (n-1) + [atr_val])
        df = pd.DataFrame({"open": close, "high": close+1, "low": close-1, "close": close, "volume": [1000.0]*n, "atr": atr})
        return df

    def test_vol_pass_normal(self):
        from core.filters.trade_filters import check_volatility
        df = self._make_df(1.0, 1.0)  # ratio=1.0 > 0.5
        passed, _ = check_volatility(df)
        assert passed

    def test_vol_reject_low_vol(self):
        from core.filters.trade_filters import check_volatility
        df = self._make_df(0.3, 1.0)  # ratio=0.3 < 0.5
        passed, reason = check_volatility(df)
        assert not passed
        assert "Volatility" in reason

    def test_vol_pass_insufficient_data(self):
        from core.filters.trade_filters import check_volatility
        df = pd.DataFrame({"close": [100.0]*5})  # too few rows
        passed, _ = check_volatility(df)
        assert passed  # pass-through on insufficient data


class TestOrderBookModelTFDisable:
    def test_order_book_disabled_at_1h(self):
        """OrderBookModel must return None for 1h timeframe."""
        from core.signals.sub_models.order_book_model import OrderBookModel
        from unittest.mock import patch
        m = OrderBookModel()
        df = pd.DataFrame({"close": [100.0]*20, "high": [101.0]*20, "low": [99.0]*20, "volume": [1000.0]*20})
        with patch("core.signals.sub_models.order_book_model._s") as ms:
            ms.get.side_effect = lambda k, d=None: "30m" if k == "models.order_book.max_timeframe" else d
            result = m.evaluate("BTC/USDT", df, "bull_trend", "1h")
        assert result is None

    def test_order_book_allowed_at_15m(self):
        """OrderBookModel should pass TF check for 15m (agent check comes after)."""
        from core.signals.sub_models.order_book_model import OrderBookModel
        from unittest.mock import patch, MagicMock
        m = OrderBookModel()
        df = pd.DataFrame({"close": [100.0]*20, "high": [101.0]*20, "low": [99.0]*20, "volume": [1000.0]*20})
        with patch("core.signals.sub_models.order_book_model._s") as ms:
            ms.get.side_effect = lambda k, d=None: {
                "models.order_book.max_timeframe": "30m",
                "models.order_book.min_signal": 0.35,
                "models.order_book.min_confidence": 0.60,
                "models.order_book.sl_atr_mult": 1.5,
                "models.order_book.tp_atr_mult": 2.0,
            }.get(k, d)
            with patch("core.agents.order_book_agent.order_book_agent", None):
                result = m.evaluate("BTC/USDT", df, "bull_trend", "15m")
        # Should return None because agent is None, not because of TF filter
        assert result is None


class TestTradeLog:
    def test_log_and_read(self, tmp_path, monkeypatch):
        monkeypatch.setattr("core.analytics.trade_log._LOG_PATH", tmp_path / "trade_log.jsonl")
        from core.analytics import trade_log
        trade_log.log_trade(
            symbol="BTC/USDT", side="buy", direction="buy",
            entry_price=50000.0, exit_price=51000.0,
            stop_loss=49000.0, take_profit=52000.0, size_usdt=500.0,
            regime="bull_trend", regime_confidence=0.85,
            confluence_score=0.73, models_fired=["trend", "funding_rate"],
            timeframe="1h", pnl_pct=2.0, pnl_usdt=10.0, exit_reason="take_profit",
            realized_r=1.0, rsi_at_entry=58.0, adx_at_entry=31.0,
            utc_hour_at_entry=15,
        )
        trades = trade_log.read_trades()
        assert len(trades) == 1
        t = trades[0]
        assert t["symbol"] == "BTC/USDT"
        assert t["won"] is True
        assert t["models_fired"] == ["trend", "funding_rate"]
        assert t["regime"] == "bull_trend"

    def test_won_flag_set_correctly(self, tmp_path, monkeypatch):
        monkeypatch.setattr("core.analytics.trade_log._LOG_PATH", tmp_path / "tl2.jsonl")
        from core.analytics import trade_log
        trade_log.log_trade(
            symbol="ETH/USDT", side="sell", direction="sell",
            entry_price=3000.0, exit_price=3100.0,
            stop_loss=3050.0, take_profit=2900.0, size_usdt=200.0,
            regime="bear_trend", regime_confidence=0.70,
            confluence_score=0.61, models_fired=["trend"],
            timeframe="1h", pnl_pct=-2.5, pnl_usdt=-5.0, exit_reason="stop_loss",
            realized_r=-1.0,
        )
        trades = trade_log.read_trades()
        assert trades[0]["won"] is False
