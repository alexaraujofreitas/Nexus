"""Tests for Model Performance Tracker (Phase 2)."""
import pytest
from unittest.mock import patch
import json


class TestModelPerformanceTracker:
    def test_record_and_get_win_rate(self, tmp_path, monkeypatch):
        monkeypatch.setattr("core.analytics.model_performance_tracker._TRACKER_PATH", tmp_path / "t.json")
        from core.analytics.model_performance_tracker import ModelPerformanceTracker
        mpt = ModelPerformanceTracker()
        for i in range(25):
            mpt.record(["trend"], won=(i < 12), realized_r=1.0 if i < 12 else -1.0)
        wr = mpt.get_win_rate("trend")
        assert wr == pytest.approx(12/25, abs=0.01)

    def test_auto_disable_threshold(self, tmp_path, monkeypatch):
        monkeypatch.setattr("core.analytics.model_performance_tracker._TRACKER_PATH", tmp_path / "t.json")
        from core.analytics.model_performance_tracker import ModelPerformanceTracker
        mpt = ModelPerformanceTracker()
        for i in range(25):
            mpt.record(["bad_model"], won=(i < 8), realized_r=-0.5)  # 32% WR
        with patch("core.analytics.model_performance_tracker._s") as ms:
            ms.get.side_effect = lambda k, d=None: {
                "filters.model_auto_disable.enabled": True,
                "filters.model_auto_disable.min_trades": 20,
                "filters.model_auto_disable.wr_threshold": 0.40,
            }.get(k, d)
            should, reason = mpt.should_auto_disable("bad_model")
        assert should
        assert "WR" in reason

    def test_no_auto_disable_insufficient_trades(self, tmp_path, monkeypatch):
        monkeypatch.setattr("core.analytics.model_performance_tracker._TRACKER_PATH", tmp_path / "t.json")
        from core.analytics.model_performance_tracker import ModelPerformanceTracker
        mpt = ModelPerformanceTracker()
        for i in range(10):
            mpt.record(["bad_model"], won=False, realized_r=-1.0)
        with patch("core.analytics.model_performance_tracker._s") as ms:
            ms.get.side_effect = lambda k, d=None: {
                "filters.model_auto_disable.enabled": True,
                "filters.model_auto_disable.min_trades": 20,
                "filters.model_auto_disable.wr_threshold": 0.40,
            }.get(k, d)
            should, _ = mpt.should_auto_disable("bad_model")
        assert not should  # Only 10 trades, need 20

    def test_get_all_stats(self, tmp_path, monkeypatch):
        monkeypatch.setattr("core.analytics.model_performance_tracker._TRACKER_PATH", tmp_path / "t.json")
        from core.analytics.model_performance_tracker import ModelPerformanceTracker
        mpt = ModelPerformanceTracker()
        mpt.record(["trend", "momentum"], won=True, realized_r=1.5)
        mpt.record(["trend"], won=False, realized_r=-1.0)
        mpt.record(["vwap"], won=True, realized_r=0.75)
        stats = mpt.get_all_stats()
        assert "trend" in stats
        assert stats["trend"]["trades"] == 2
        assert stats["trend"]["wins"] == 1

    def test_regime_stats(self, tmp_path, monkeypatch):
        monkeypatch.setattr("core.analytics.model_performance_tracker._TRACKER_PATH", tmp_path / "t.json")
        from core.analytics.model_performance_tracker import ModelPerformanceTracker
        mpt = ModelPerformanceTracker()
        mpt.record(["trend"], won=True, realized_r=1.0, regime="bull_trend")
        mpt.record(["trend"], won=True, realized_r=1.0, regime="bull_trend")
        mpt.record(["trend"], won=False, realized_r=-1.0, regime="ranging")
        wr_bull = mpt.get_regime_win_rate("trend", "bull_trend")
        assert wr_bull == pytest.approx(1.0)

    def test_persistence(self, tmp_path, monkeypatch):
        monkeypatch.setattr("core.analytics.model_performance_tracker._TRACKER_PATH", tmp_path / "t.json")
        from core.analytics.model_performance_tracker import ModelPerformanceTracker
        mpt1 = ModelPerformanceTracker()
        mpt1.record(["trend"], won=True, realized_r=1.0)
        # Create new instance — should load previous data
        mpt2 = ModelPerformanceTracker()
        wr = mpt2.get_win_rate("trend")
        assert wr == 1.0

    def test_get_models_to_disable(self, tmp_path, monkeypatch):
        monkeypatch.setattr("core.analytics.model_performance_tracker._TRACKER_PATH", tmp_path / "t.json")
        from core.analytics.model_performance_tracker import ModelPerformanceTracker
        mpt = ModelPerformanceTracker()
        # Create underperforming model
        for i in range(25):
            mpt.record(["bad"], won=(i < 8), realized_r=-0.5)
        # Create good model
        for i in range(25):
            mpt.record(["good"], won=(i < 15), realized_r=0.5)
        with patch("core.analytics.model_performance_tracker._s") as ms:
            ms.get.side_effect = lambda k, d=None: {
                "filters.model_auto_disable.enabled": True,
                "filters.model_auto_disable.min_trades": 20,
                "filters.model_auto_disable.wr_threshold": 0.40,
            }.get(k, d)
            to_disable = mpt.get_models_to_disable()
        assert len(to_disable) == 1
        assert to_disable[0][0] == "bad"

    def test_reset_single_model(self, tmp_path, monkeypatch):
        monkeypatch.setattr("core.analytics.model_performance_tracker._TRACKER_PATH", tmp_path / "t.json")
        from core.analytics.model_performance_tracker import ModelPerformanceTracker
        mpt = ModelPerformanceTracker()
        mpt.record(["trend"], won=True, realized_r=1.0)
        mpt.record(["momentum"], won=False, realized_r=-1.0)
        mpt.reset("trend")
        assert mpt.get_win_rate("trend") is None
        assert mpt.get_win_rate("momentum") is not None

    def test_reset_all(self, tmp_path, monkeypatch):
        monkeypatch.setattr("core.analytics.model_performance_tracker._TRACKER_PATH", tmp_path / "t.json")
        from core.analytics.model_performance_tracker import ModelPerformanceTracker
        mpt = ModelPerformanceTracker()
        mpt.record(["trend", "momentum"], won=True, realized_r=1.0)
        mpt.reset()
        assert mpt.get_all_stats() == {}
