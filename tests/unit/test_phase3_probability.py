"""Tests for Phase 3 — Feature Extractor and Probability Calibrator."""
import pytest


class TestTradeFeatureExtractor:
    def test_extract_from_trade_dict(self):
        from core.learning.trade_feature_extractor import extract_features_from_trade
        trade = {
            "regime": "bull_trend", "primary_model": "trend",
            "side": "buy", "confluence_score": 0.72,
            "rsi_at_entry": 58.0, "adx_at_entry": 32.0,
            "atr_ratio": 1.1, "funding_rate": 0.01,
            "utc_hour_at_entry": 15, "models_fired": ["trend"],
        }
        feats = extract_features_from_trade(trade)
        assert feats is not None
        assert feats["confluence_score"] == pytest.approx(0.72)
        assert feats["regime_bull_trend"] == 1
        assert feats["regime_bear_trend"] == 0
        assert feats["model_trend"] == 1
        assert feats["is_long"] == 1

    def test_extract_live(self):
        from core.learning.trade_feature_extractor import extract_features_live
        feats = extract_features_live(
            regime="ranging", confluence_score=0.65,
            direction="sell", models_fired=["vwap_reversion"],
            rsi=45.0, adx=18.0, atr_ratio=0.9, utc_hour=16,
        )
        assert feats["regime_ranging"] == 1
        assert feats["is_long"] == 0
        assert feats["model_vwap_reversion"] == 1

    def test_build_training_dataset(self):
        from core.learning.trade_feature_extractor import build_training_dataset
        trades = [
            {"regime": "bull_trend", "primary_model": "trend", "side": "buy",
             "confluence_score": 0.7, "won": True, "models_fired": ["trend"]},
            {"regime": "ranging", "primary_model": "vwap_reversion", "side": "sell",
             "confluence_score": 0.6, "won": False, "models_fired": ["vwap_reversion"]},
        ]
        X, y = build_training_dataset(trades)
        assert len(X) == 2
        assert y == [1, 0]


class TestProbabilityCalibrator:
    def test_sigmoid_fallback_when_not_trained(self, tmp_path, monkeypatch):
        monkeypatch.setattr("core.learning.probability_calibrator._MODEL_PATH", tmp_path / "m.pkl")
        monkeypatch.setattr("core.learning.probability_calibrator._CALIBRATION_PATH", tmp_path / "c.json")
        from core.learning.probability_calibrator import ProbabilityCalibrator
        cal = ProbabilityCalibrator()
        assert not cal.is_trained()
        prob, source = cal.get_win_prob({}, score=0.55)
        assert source == "sigmoid"
        assert 0.45 <= prob <= 0.55  # near 0.50 at midpoint

    def test_sigmoid_calibration_correct(self, tmp_path, monkeypatch):
        monkeypatch.setattr("core.learning.probability_calibrator._MODEL_PATH", tmp_path / "m.pkl")
        monkeypatch.setattr("core.learning.probability_calibrator._CALIBRATION_PATH", tmp_path / "c.json")
        from core.learning.probability_calibrator import ProbabilityCalibrator
        cal = ProbabilityCalibrator()
        prob_high, _ = cal.get_win_prob({}, score=0.80, sigmoid_k=8.0, sigmoid_midpoint=0.55)
        prob_low, _  = cal.get_win_prob({}, score=0.30, sigmoid_k=8.0, sigmoid_midpoint=0.55)
        assert prob_high > 0.7
        assert prob_low < 0.3

    def test_score_bucketing(self, tmp_path, monkeypatch):
        monkeypatch.setattr("core.learning.probability_calibrator._MODEL_PATH", tmp_path / "m.pkl")
        monkeypatch.setattr("core.learning.probability_calibrator._CALIBRATION_PATH", tmp_path / "c.json")
        from core.learning.probability_calibrator import ProbabilityCalibrator
        cal = ProbabilityCalibrator()
        trades = [
            {"confluence_score": 0.72, "pnl_pct": 1.5},
            {"confluence_score": 0.68, "pnl_pct": 0.8},
            {"confluence_score": 0.45, "pnl_pct": -1.2},
        ]
        result = cal.compute_score_calibration(trades)
        assert "0.65-0.70" in result
        assert "0.40-0.50" in result

    def test_predict_proba_raises_when_not_trained(self, tmp_path, monkeypatch):
        monkeypatch.setattr("core.learning.probability_calibrator._MODEL_PATH", tmp_path / "m.pkl")
        monkeypatch.setattr("core.learning.probability_calibrator._CALIBRATION_PATH", tmp_path / "c.json")
        from core.learning.probability_calibrator import ProbabilityCalibrator
        cal = ProbabilityCalibrator()
        with pytest.raises(ValueError):
            cal.predict_proba({})

    def test_get_win_prob_handles_missing_features(self, tmp_path, monkeypatch):
        monkeypatch.setattr("core.learning.probability_calibrator._MODEL_PATH", tmp_path / "m.pkl")
        monkeypatch.setattr("core.learning.probability_calibrator._CALIBRATION_PATH", tmp_path / "c.json")
        from core.learning.probability_calibrator import ProbabilityCalibrator
        cal = ProbabilityCalibrator()
        # Should return sigmoid (no error) even if features incomplete
        prob, source = cal.get_win_prob({}, score=0.65)
        assert source == "sigmoid"
        assert 0.0 <= prob <= 1.0
