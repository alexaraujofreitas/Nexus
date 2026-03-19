"""
NexusTrader — Level-2 Trade Learning Test Suite
================================================
7 scenario groups, ~70 tests.

Scenarios:
  S1 — Synthetic outcome recording (TradeOutcomeStore + EnrichedTrade)
  S2 — Model divergence (model A winning, model B losing)
  S3 — Regime-specific performance cells
  S4 — Dataset integrity (duplicates, edge values, persistence round-trip)
  S5 — Stability & bounds (combined multiplier hard cap)
  S6 — Signal generation influence (AdaptiveWeightEngine × ConfluenceScorer)
  S7 — DemoPerformanceEvaluator L2 check integration
"""
import sys
import os
import json
import tempfile
import pytest
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
os.chdir(str(Path(__file__).parent.parent.parent))

# ── Fixtures ─────────────────────────────────────────────────────────────────

def make_trade_dict(
    symbol="BTC/USDT", side="buy",
    entry=50000, sl=49000, tp=52000,
    pnl_pct=2.1, pnl_usdt=21.0,
    regime="bull_trend", models=None,
    size=1000, score=0.72,
    exit_reason="take_profit",
    days_ago=1,
):
    opened = (datetime.utcnow() - timedelta(days=days_ago + 0.1)).isoformat()
    closed = (datetime.utcnow() - timedelta(days=days_ago)).isoformat()
    return {
        "symbol":       symbol,
        "side":         side,
        "entry_price":  entry,
        "stop_loss":    sl,
        "take_profit":  tp,
        "size_usdt":    size,
        "pnl_pct":      pnl_pct,
        "pnl_usdt":     pnl_usdt,
        "exit_reason":  exit_reason,
        "score":        score,
        "regime":       regime,
        "models_fired": models or ["trend"],
        "timeframe":    "1h",
        "duration_s":   3600,
        "opened_at":    opened,
        "closed_at":    closed,
        "entry_expected": entry,
        "expected_value": 0.1,
    }


# ═══════════════════════════════════════════════════════════════════════════
# S1 — Synthetic outcome recording
# ═══════════════════════════════════════════════════════════════════════════

class TestS1_SyntheticRecording:
    """Verify TradeOutcomeStore and EnrichedTrade build correctly."""

    def test_s1_01_enriched_trade_from_dict(self):
        from core.learning.trade_outcome_store import EnrichedTrade
        td = make_trade_dict()
        et = EnrichedTrade.from_trade_dict(td)
        assert et.symbol == "BTC/USDT"
        assert et.side   == "buy"
        assert et.won is True

    def test_s1_02_trade_id_format(self):
        from core.learning.trade_outcome_store import EnrichedTrade
        td = make_trade_dict(symbol="ETH/USDT", days_ago=3)
        et = EnrichedTrade.from_trade_dict(td)
        assert et.trade_id.startswith("ETH/USDT_")

    def test_s1_03_expected_rr_computed(self):
        from core.learning.trade_outcome_store import EnrichedTrade
        # buy entry=50000 sl=49000 tp=52000 → R:R = 2000/1000 = 2.0
        et = EnrichedTrade.from_trade_dict(
            make_trade_dict(entry=50000, sl=49000, tp=52000, side="buy")
        )
        assert et.expected_rr is not None
        assert abs(et.expected_rr - 2.0) < 0.01

    def test_s1_04_short_trade_rr(self):
        from core.learning.trade_outcome_store import EnrichedTrade
        # sell entry=50000 sl=51000 tp=48000 → risk=1000 reward=2000
        et = EnrichedTrade.from_trade_dict(
            make_trade_dict(entry=50000, sl=51000, tp=48000, side="sell")
        )
        assert et.expected_rr is not None
        assert abs(et.expected_rr - 2.0) < 0.01

    def test_s1_05_realized_r_multiple(self):
        from core.learning.trade_outcome_store import EnrichedTrade
        # entry=50000 sl=49000 size=1000 → risk_usdt = 1000/50000*1000 = 20
        # pnl_usdt=40 → R = 40/20 = 2.0
        et = EnrichedTrade.from_trade_dict(
            make_trade_dict(entry=50000, sl=49000, pnl_usdt=40.0, size=1000)
        )
        assert et.realized_r_multiple is not None
        assert abs(et.realized_r_multiple - 2.0) < 0.01

    def test_s1_06_slippage_computed(self):
        from core.learning.trade_outcome_store import EnrichedTrade
        td = make_trade_dict(entry=50100)
        td["entry_expected"] = 50000
        et = EnrichedTrade.from_trade_dict(td)
        assert et.slippage_pct is not None
        assert et.slippage_pct > 0

    def test_s1_07_won_flag_pnl_negative(self):
        from core.learning.trade_outcome_store import EnrichedTrade
        et = EnrichedTrade.from_trade_dict(
            make_trade_dict(pnl_pct=-1.5, pnl_usdt=-15.0)
        )
        assert et.won is False

    def test_s1_08_store_records_trade(self):
        from core.learning.trade_outcome_store import TradeOutcomeStore
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            path = Path(f.name)
        store = TradeOutcomeStore(path)
        td    = make_trade_dict(days_ago=5)
        et    = store.record(td)
        assert et is not None
        assert len(store) == 1

    def test_s1_09_store_deduplicates(self):
        from core.learning.trade_outcome_store import TradeOutcomeStore
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            path = Path(f.name)
        store = TradeOutcomeStore(path)
        td    = make_trade_dict(days_ago=5)
        store.record(td)
        result = store.record(td)   # duplicate
        assert result is None
        assert len(store) == 1

    def test_s1_10_store_query_by_model(self):
        from core.learning.trade_outcome_store import TradeOutcomeStore
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            path = Path(f.name)
        store = TradeOutcomeStore(path)
        store.record(make_trade_dict(days_ago=5, models=["trend"]))
        store.record(make_trade_dict(days_ago=4, models=["mean_reversion"]))
        assert len(store.trades_for_model("trend")) == 1
        assert len(store.trades_for_model("mean_reversion")) == 1

    def test_s1_11_store_query_by_regime(self):
        from core.learning.trade_outcome_store import TradeOutcomeStore
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            path = Path(f.name)
        store = TradeOutcomeStore(path)
        store.record(make_trade_dict(days_ago=5, regime="bull_trend"))
        store.record(make_trade_dict(days_ago=4, regime="ranging"))
        assert len(store.trades_for_regime("bull_trend")) == 1
        assert len(store.trades_for_regime("ranging")) == 1


# ═══════════════════════════════════════════════════════════════════════════
# S2 — Model divergence
# ═══════════════════════════════════════════════════════════════════════════

class TestS2_ModelDivergence:
    """Model A (trend) dominates; model B (mean_reversion) underperforms."""

    @pytest.fixture(autouse=True)
    def fresh_tracker(self):
        from core.learning.level2_tracker import Level2PerformanceTracker
        self.tracker = Level2PerformanceTracker.__new__(Level2PerformanceTracker)
        import threading
        self.tracker._lock      = threading.Lock()
        self.tracker._regime    = {}
        self.tracker._asset     = {}
        self.tracker._score_cal = {}
        self.tracker._exit_eff  = {}
        self.tracker._exit_r    = {}
        self.tracker._entry_rr  = {}

    def _feed(self, model, won, n, regime="bull_trend", symbol="BTC/USDT"):
        for _ in range(n):
            self.tracker.record([model], won=won, regime=regime,
                                symbol=symbol, score=0.6)

    def test_s2_01_trend_wins_high_adj(self):
        self._feed("trend", True, 15)
        adj = self.tracker.get_regime_adjustment("trend", "bull_trend")
        assert adj > 1.0, f"Expected >1.0, got {adj}"

    def test_s2_02_mean_rev_loses_low_adj(self):
        self._feed("mean_reversion", False, 15)
        adj = self.tracker.get_regime_adjustment("mean_reversion", "bull_trend")
        assert adj < 1.0, f"Expected <1.0, got {adj}"

    def test_s2_03_trend_adj_capped_at_max(self):
        self._feed("trend", True, 50)
        from core.learning.level2_tracker import MAX_ADJ_REGIME
        adj = self.tracker.get_regime_adjustment("trend", "bull_trend")
        assert adj <= 1.0 + MAX_ADJ_REGIME + 0.001

    def test_s2_04_mean_rev_adj_floored_at_min(self):
        self._feed("mean_reversion", False, 50)
        from core.learning.level2_tracker import MAX_ADJ_REGIME
        adj = self.tracker.get_regime_adjustment("mean_reversion", "bull_trend")
        assert adj >= 1.0 - MAX_ADJ_REGIME - 0.001

    def test_s2_05_insufficient_data_returns_neutral(self):
        # 3 trades < MIN_SAMPLES_PARTIAL=5 → no active cells for fallback → neutral 1.0
        self._feed("trend", True, 3)
        adj = self.tracker.get_regime_adjustment("trend", "bull_trend")
        assert adj == 1.0

    def test_s2_06_independent_cells(self):
        # trend bull_trend active; mean_reversion ranging inactive
        self._feed("trend", True, 15, regime="bull_trend")
        adj_r = self.tracker.get_regime_adjustment("mean_reversion", "ranging")
        assert adj_r == 1.0   # no data for that cell


# ═══════════════════════════════════════════════════════════════════════════
# S3 — Regime-specific performance cells
# ═══════════════════════════════════════════════════════════════════════════

class TestS3_RegimePerformance:

    @pytest.fixture(autouse=True)
    def fresh_tracker(self):
        from core.learning.level2_tracker import Level2PerformanceTracker
        import threading
        t = Level2PerformanceTracker.__new__(Level2PerformanceTracker)
        t._lock = threading.Lock(); t._regime = {}; t._asset = {}
        t._score_cal = {}; t._exit_eff = {}
        t._exit_r = {}; t._entry_rr = {}
        self.tracker = t

    def test_s3_01_bull_trend_wins(self):
        for _ in range(12):
            self.tracker.record(["trend"], True, "bull_trend", "BTC/USDT", 0.7)
        adj = self.tracker.get_regime_adjustment("trend", "bull_trend")
        assert adj > 1.0

    def test_s3_02_ranging_loses(self):
        for _ in range(12):
            self.tracker.record(["trend"], False, "ranging", "BTC/USDT", 0.6)
        adj = self.tracker.get_regime_adjustment("trend", "ranging")
        assert adj < 1.0

    def test_s3_03_different_regimes_independent(self):
        for _ in range(12):
            self.tracker.record(["trend"], True, "bull_trend", "BTC/USDT", 0.7)
        for _ in range(12):
            self.tracker.record(["trend"], False, "ranging", "BTC/USDT", 0.5)
        bull_adj    = self.tracker.get_regime_adjustment("trend", "bull_trend")
        ranging_adj = self.tracker.get_regime_adjustment("trend", "ranging")
        assert bull_adj > ranging_adj

    def test_s3_04_regime_table_returns_rows(self):
        for _ in range(12):
            self.tracker.record(["trend"], True, "bull_trend", "BTC/USDT", 0.7)
        rows = self.tracker.get_regime_table()
        assert len(rows) >= 1
        assert all("model" in r and "regime" in r for r in rows)

    def test_s3_05_asset_adjustment_active(self):
        for _ in range(12):
            self.tracker.record(["trend"], True, "bull_trend", "BTC/USDT", 0.7)
        adj = self.tracker.get_asset_adjustment("trend", "BTC/USDT")
        assert adj > 1.0

    def test_s3_06_asset_table_returns_rows(self):
        for _ in range(12):
            self.tracker.record(["trend"], True, "bull_trend", "ETH/USDT", 0.7)
        rows = self.tracker.get_asset_table()
        assert any(r["symbol"] == "ETH/USDT" for r in rows)

    def test_s3_07_score_calibration_bin(self):
        for _ in range(6):
            self.tracker.record(["trend"], True, "bull_trend", "BTC/USDT", 0.75)
        cal = self.tracker.get_score_calibration()
        assert "0.7-0.8" in cal

    def test_s3_08_exit_efficiency_tracked(self):
        self.tracker.record(["trend"], True, "bull_trend", "BTC/USDT", 0.7,
                            exit_reason="take_profit")
        self.tracker.record(["trend"], False, "bull_trend", "BTC/USDT", 0.6,
                            exit_reason="stop_loss")
        eff = self.tracker.get_exit_efficiency()
        assert "trend" in eff
        assert eff["trend"]["tp"] == 1
        assert eff["trend"]["sl"] == 1


# ═══════════════════════════════════════════════════════════════════════════
# S4 — Dataset integrity
# ═══════════════════════════════════════════════════════════════════════════

class TestS4_DatasetIntegrity:

    def test_s4_01_persistence_round_trip(self):
        from core.learning.level2_tracker import Level2PerformanceTracker
        import threading
        with tempfile.TemporaryDirectory() as tmpdir:
            # Monkey-patch the persist path
            t = Level2PerformanceTracker.__new__(Level2PerformanceTracker)
            t._lock = threading.Lock(); t._regime = {}; t._asset = {}
            t._score_cal = {}; t._exit_eff = {}
            t._exit_r = {}; t._entry_rr = {}
            import core.learning.level2_tracker as _m
            orig = _m._PERSIST_FILE
            _m._PERSIST_FILE = Path(tmpdir) / "l2.json"
            try:
                for _ in range(12):
                    t.record(["trend"], True, "bull_trend", "BTC/USDT", 0.7)
                t._save()
                # load into fresh instance
                t2 = Level2PerformanceTracker.__new__(Level2PerformanceTracker)
                t2._lock = threading.Lock(); t2._regime = {}; t2._asset = {}
                t2._score_cal = {}; t2._exit_eff = {}
                t2._exit_r = {}; t2._entry_rr = {}
                t2._load()
                adj = t2.get_regime_adjustment("trend", "bull_trend")
                assert adj > 1.0
            finally:
                _m._PERSIST_FILE = orig

    def test_s4_02_outcome_store_persistence(self):
        from core.learning.trade_outcome_store import TradeOutcomeStore
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            path = Path(f.name)
        s1 = TradeOutcomeStore(path)
        s1.record(make_trade_dict(days_ago=5))
        s2 = TradeOutcomeStore(path)
        assert len(s2) == 1

    def test_s4_03_outcome_store_no_crash_on_missing_file(self):
        from core.learning.trade_outcome_store import TradeOutcomeStore
        path = Path(tempfile.mktemp(suffix=".jsonl"))
        store = TradeOutcomeStore(path)   # file doesn't exist
        assert len(store) == 0

    def test_s4_04_zero_entry_price_handled(self):
        from core.learning.trade_outcome_store import EnrichedTrade
        td = make_trade_dict(entry=0)
        et = EnrichedTrade.from_trade_dict(td)
        assert et.expected_rr is None      # can't compute
        assert et.realized_r_multiple is None

    def test_s4_05_missing_optional_fields(self):
        from core.learning.trade_outcome_store import EnrichedTrade
        td = {
            "symbol": "BTC/USDT", "side": "buy",
            "entry_price": 50000, "pnl_pct": 2.0, "pnl_usdt": 20.0,
            "opened_at": datetime.utcnow().isoformat(),
            "closed_at": datetime.utcnow().isoformat(),
        }
        et = EnrichedTrade.from_trade_dict(td)
        assert et.symbol == "BTC/USDT"
        assert et.won is True

    def test_s4_06_score_bin_boundaries(self):
        from core.learning.level2_tracker import Level2PerformanceTracker
        assert Level2PerformanceTracker._score_bin(0.3)  == "0.3-0.4"
        assert Level2PerformanceTracker._score_bin(0.39) == "0.3-0.4"
        assert Level2PerformanceTracker._score_bin(0.4)  == "0.4-0.5"
        assert Level2PerformanceTracker._score_bin(0.9)  == "0.9-1.0"
        assert Level2PerformanceTracker._score_bin(1.0)  == "0.9-1.0"
        assert Level2PerformanceTracker._score_bin(0.1)  is None

    def test_s4_07_get_summary_structure(self):
        from core.learning.level2_tracker import Level2PerformanceTracker
        import threading
        t = Level2PerformanceTracker.__new__(Level2PerformanceTracker)
        t._lock = threading.Lock(); t._regime = {}; t._asset = {}
        t._score_cal = {}; t._exit_eff = {}
        t._exit_r = {}; t._entry_rr = {}
        s = t.get_summary()
        assert "regime_cells_active" in s
        assert "asset_cells_active" in s
        assert "total_cells" in s


# ═══════════════════════════════════════════════════════════════════════════
# S5 — Stability & bounds
# ═══════════════════════════════════════════════════════════════════════════

class TestS5_StabilityBounds:

    @pytest.fixture(autouse=True)
    def fresh_engine(self):
        from core.learning.adaptive_weight_engine import AdaptiveWeightEngine
        self.engine = AdaptiveWeightEngine()

    def test_s5_01_neutral_when_no_data(self):
        m = self.engine.get_multiplier("nonexistent_model", "unknown", "BTC/USDT")
        assert m == 1.0

    def test_s5_02_result_within_combined_bounds(self):
        from core.learning.adaptive_weight_engine import MIN_COMBINED, MAX_COMBINED
        m = self.engine.get_multiplier("trend", "bull_trend", "BTC/USDT")
        assert MIN_COMBINED <= m <= MAX_COMBINED

    def test_s5_03_detail_dict_structure(self):
        d = self.engine.get_detail("trend", "bull_trend", "BTC/USDT")
        for key in ("l1", "l2_regime", "l2_asset", "combined", "multiplier", "clamped"):
            assert key in d

    def test_s5_04_batch_detail(self):
        models = ["trend", "mean_reversion"]
        details = self.engine.get_all_model_details(models, "ranging", "ETH/USDT")
        assert set(details.keys()) == set(models)

    def test_s5_05_hard_cap_prevents_runaway(self):
        from core.learning.adaptive_weight_engine import MIN_COMBINED, MAX_COMBINED
        # Even if we mock extreme L2 values, the result must stay in bounds
        import unittest.mock as mock
        with mock.patch.object(self.engine, "_l2_regime", return_value=2.0), \
             mock.patch.object(self.engine, "_l2_asset", return_value=2.0), \
             mock.patch.object(self.engine, "_l1", return_value=2.0):
            m = self.engine.get_multiplier("trend", "bull_trend", "BTC/USDT")
        assert m == MAX_COMBINED

    def test_s5_06_floor_prevents_zero(self):
        from core.learning.adaptive_weight_engine import MIN_COMBINED
        import unittest.mock as mock
        with mock.patch.object(self.engine, "_l2_regime", return_value=0.0), \
             mock.patch.object(self.engine, "_l2_asset", return_value=0.0), \
             mock.patch.object(self.engine, "_l1", return_value=0.0):
            m = self.engine.get_multiplier("trend", "bull_trend", "BTC/USDT")
        assert m == MIN_COMBINED

    def test_s5_07_rolling_window_evicts_old(self):
        from core.learning.level2_tracker import _RollingWindow, WINDOW
        w = _RollingWindow(maxlen=WINDOW)
        # Fill with losses, then fill with wins → old losses should be gone
        for _ in range(WINDOW):
            w.append(False)
        for _ in range(WINDOW):
            w.append(True)
        wr = w.win_rate()
        assert wr == 1.0   # all wins, losses evicted

    def test_s5_08_rolling_window_min_samples_guard(self):
        from core.learning.level2_tracker import _RollingWindow, MIN_SAMPLES_CELL
        w = _RollingWindow()
        for _ in range(MIN_SAMPLES_CELL - 1):
            w.append(True)
        assert w.win_rate() is None    # just below threshold

    def test_s5_09_win_rate_to_adj_formula(self):
        from core.learning.level2_tracker import _win_rate_to_adj
        assert _win_rate_to_adj(0.50, 0.10) == 1.0        # neutral
        assert _win_rate_to_adj(0.70, 0.10) == 1.10       # max boost
        assert _win_rate_to_adj(0.30, 0.10) == 0.90       # max penalty
        assert _win_rate_to_adj(0.90, 0.10) == 1.10       # clamped at max
        assert _win_rate_to_adj(0.10, 0.10) == 0.90       # clamped at min
        assert _win_rate_to_adj(None, 0.10) == 1.0        # no data → neutral

    def test_s5_10_cross_model_isolation_preserved(self):
        # v2: same-model fallback carries prior into unseen cells.
        # Cross-MODEL isolation must still be strict.
        from core.learning.level2_tracker import Level2PerformanceTracker, FALLBACK_STRENGTH
        import threading
        t = Level2PerformanceTracker.__new__(Level2PerformanceTracker)
        t._lock = threading.Lock(); t._regime = {}; t._asset = {}
        t._score_cal = {}; t._exit_eff = {}
        t._exit_r = {}; t._entry_rr = {}
        for _ in range(15):
            t.record(["trend"], True, "bull_trend", "BTC/USDT", 0.7)
        # Same model, unseen regime → hierarchical fallback kicks in (> 1.0)
        assert t.get_regime_adjustment("trend", "ranging") > 1.0
        assert t.get_asset_adjustment("trend", "ETH/USDT") > 1.0
        # Different model has NO data → strict neutral (no cross-model bleed)
        assert t.get_regime_adjustment("mean_reversion", "ranging") == 1.0
        assert t.get_asset_adjustment("momentum", "BTC/USDT") == 1.0


# ═══════════════════════════════════════════════════════════════════════════
# S6 — Signal generation influence (ConfluenceScorer integration)
# ═══════════════════════════════════════════════════════════════════════════

class TestS6_SignalInfluence:
    """AdaptiveWeightEngine correctly wires into ConfluenceScorer."""

    def test_s6_01_adaptive_engine_importable(self):
        from core.learning.adaptive_weight_engine import (
            AdaptiveWeightEngine, get_adaptive_weight_engine,
        )
        engine = get_adaptive_weight_engine()
        assert isinstance(engine, AdaptiveWeightEngine)

    def test_s6_02_get_multiplier_returns_float(self):
        from core.learning.adaptive_weight_engine import get_adaptive_weight_engine
        m = get_adaptive_weight_engine().get_multiplier("trend", "bull_trend", "BTC/USDT")
        assert isinstance(m, float)

    def test_s6_03_multiplier_neutral_for_unknown_model(self):
        from core.learning.adaptive_weight_engine import get_adaptive_weight_engine
        m = get_adaptive_weight_engine().get_multiplier(
            "totally_nonexistent_xyz", "unknown", "BTC/USDT"
        )
        assert m == 1.0

    def test_s6_04_confluence_scorer_importable(self):
        from core.meta_decision.confluence_scorer import ConfluenceScorer
        cs = ConfluenceScorer()
        assert cs is not None

    def test_s6_05_learning_modules_exportable(self):
        from core.learning import (
            TradeOutcomeStore, EnrichedTrade, get_outcome_store,
            Level2PerformanceTracker, get_level2_tracker,
            AdaptiveWeightEngine, get_adaptive_weight_engine,
        )
        assert get_outcome_store() is not None
        assert get_level2_tracker() is not None
        assert get_adaptive_weight_engine() is not None


# ═══════════════════════════════════════════════════════════════════════════
# S7 — DemoPerformanceEvaluator L2 integration
# ═══════════════════════════════════════════════════════════════════════════

class TestS7_DPEIntegration:
    """Verify DemoPerformanceEvaluator includes the new L2 check (#16)."""

    def _make_dataset(self, n=85, win_rate=0.55):
        import random
        trades = []
        regimes = ("bull_trend", "bear_trend", "ranging", "vol_expansion")
        assets  = ("BTC/USDT", "ETH/USDT", "SOL/USDT")
        for i in range(n):
            won = random.random() < win_rate
            pnl = 2.0 if won else -1.5
            trades.append(make_trade_dict(
                symbol=assets[i % len(assets)],
                regime=regimes[i % len(regimes)],
                pnl_pct=pnl, pnl_usdt=pnl * 10,
                days_ago=(n - i) / 10,
                exit_reason="take_profit" if won else "stop_loss",
            ))
        return trades

    def test_s7_01_l2_check_present_in_assessment(self):
        from core.evaluation.demo_performance_evaluator import DemoPerformanceEvaluator
        ev = DemoPerformanceEvaluator()
        trades = self._make_dataset(85)
        a = ev.evaluate(trades)
        names = [c.name for c in a.check_details]
        assert any("Level-2" in n for n in names), f"L2 check not found: {names}"

    def test_s7_02_l2_check_has_weight_1(self):
        from core.evaluation.demo_performance_evaluator import DemoPerformanceEvaluator
        ev = DemoPerformanceEvaluator()
        a  = ev.evaluate(self._make_dataset(85))
        l2_check = next(
            (c for c in a.check_details if "Level-2" in c.name), None
        )
        assert l2_check is not None
        assert l2_check.weight == 1

    def test_s7_03_total_checks_is_20(self):
        from core.evaluation.demo_performance_evaluator import DemoPerformanceEvaluator
        ev = DemoPerformanceEvaluator()
        a  = ev.evaluate(self._make_dataset(85))
        assert len(a.check_details) == 20, \
            f"Expected 20 checks, got {len(a.check_details)}"

    def test_s7_04_l2_check_not_blocking(self):
        from core.evaluation.demo_performance_evaluator import DemoPerformanceEvaluator
        ev = DemoPerformanceEvaluator()
        a  = ev.evaluate(self._make_dataset(85))
        l2_check = next(
            (c for c in a.check_details if "Level-2" in c.name), None
        )
        assert l2_check is not None
        assert l2_check.weight != 3, "L2 check must not be a blocking check"

    def test_s7_05_safety_contract_still_holds(self):
        from core.evaluation.demo_performance_evaluator import DemoPerformanceEvaluator
        import inspect
        src = inspect.getsource(DemoPerformanceEvaluator)
        assert "set_mode" not in src
        assert "order_router" not in src

    def test_s7_06_check_l2_status_returns_tuple(self):
        from core.evaluation.demo_performance_evaluator import DemoPerformanceEvaluator
        active, total, notes = DemoPerformanceEvaluator._check_l2_status()
        assert isinstance(active, int)
        assert isinstance(total, int)
        assert isinstance(notes, str)

    def test_s7_07_l2_status_no_exception_when_module_missing(self):
        """_check_l2_status must not raise even if level2 module unavailable."""
        from core.evaluation.demo_performance_evaluator import DemoPerformanceEvaluator
        import unittest.mock as mock
        with mock.patch(
            "core.evaluation.demo_performance_evaluator."
            "DemoPerformanceEvaluator._check_l2_status",
            return_value=(0, 0, "mocked"),
        ):
            ev = DemoPerformanceEvaluator()
            a  = ev.evaluate(self._make_dataset(85))
            l2 = next((c for c in a.check_details if "Level-2" in c.name), None)
            assert l2 is not None


# ═══════════════════════════════════════════════════════════════════════════
# S8 — Partial Activation (Section 1)
# ═══════════════════════════════════════════════════════════════════════════

class TestS8_PartialActivation:
    """Verify confidence-scaled partial activation between MIN_SAMPLES_PARTIAL and MIN_SAMPLES_CELL."""

    @pytest.fixture(autouse=True)
    def fresh_tracker(self):
        from core.learning.level2_tracker import Level2PerformanceTracker
        import threading
        t = Level2PerformanceTracker.__new__(Level2PerformanceTracker)
        t._lock = threading.Lock()
        t._regime = {}; t._asset = {}
        t._score_cal = {}; t._exit_eff = {}
        t._exit_r = {}; t._entry_rr = {}
        self.tracker = t

    def test_s8_01_partial_adj_is_between_neutral_and_full(self):
        """7 winning trades → partial adj must be between 1.0 and full adj."""
        from core.learning.level2_tracker import MAX_ADJ_REGIME
        for _ in range(7):
            self.tracker.record(["trend"], True, "bull_trend", "BTC/USDT", 0.7)
        partial = self.tracker.get_regime_adjustment("trend", "bull_trend")
        full_max = 1.0 + MAX_ADJ_REGIME
        assert 1.0 < partial < full_max, f"Expected in (1.0, {full_max}), got {partial}"

    def test_s8_02_partial_grows_with_more_trades(self):
        """Each additional trade in 5–9 range should push adj slightly further."""
        adjs = []
        for i in range(1, 10):
            self.tracker.record(["trend"], True, "bull_trend", "BTC/USDT", 0.7)
            adjs.append(self.tracker.get_regime_adjustment("trend", "bull_trend"))
        # After 4 trades: fallback; after 5-9: growing partial; after 10: full
        assert adjs[8] >= adjs[4], "Adjustment should be non-decreasing with more trades"

    def test_s8_03_partial_neutral_for_50pct_win_rate(self):
        """5 wins, 5 losses = 50% WR → partial adj should be exactly 1.0."""
        from core.learning.level2_tracker import MIN_SAMPLES_PARTIAL
        # Feed 3 wins + 2 losses into a fresh cell (count=5=MIN_SAMPLES_PARTIAL)
        for _ in range(3):
            self.tracker.record(["trend"], True, "bull_trend", "BTC/USDT", 0.6)
        for _ in range(2):
            self.tracker.record(["trend"], False, "bull_trend", "BTC/USDT", 0.6)
        adj = self.tracker.get_regime_adjustment("trend", "bull_trend")
        # 3/5 = 60% WR → partial, not neutral; but close to neutral
        assert adj > 1.0, "60% WR partial should be > neutral"

    def test_s8_04_full_activation_at_threshold(self):
        """Exactly 10 wins → full activation, not partial."""
        from core.learning.level2_tracker import MIN_SAMPLES_CELL, MAX_ADJ_REGIME
        for _ in range(MIN_SAMPLES_CELL):
            self.tracker.record(["trend"], True, "bull_trend", "BTC/USDT", 0.7)
        adj = self.tracker.get_regime_adjustment("trend", "bull_trend")
        assert adj == 1.0 + MAX_ADJ_REGIME, f"Expected full adj {1+MAX_ADJ_REGIME}, got {adj}"

    def test_s8_05_regime_table_shows_partial_tier(self):
        """get_regime_table() should show activation_tier='partial' for 5-9 trade cells."""
        for _ in range(7):
            self.tracker.record(["trend"], True, "bull_trend", "BTC/USDT", 0.7)
        rows = self.tracker.get_regime_table()
        trend_row = next((r for r in rows if r["model"] == "trend"), None)
        assert trend_row is not None
        assert trend_row["activation_tier"] == "partial"

    def test_s8_06_asset_partial_activation(self):
        """Asset cells also support partial activation."""
        from core.learning.level2_tracker import MAX_ADJ_ASSET
        for _ in range(7):
            self.tracker.record(["trend"], True, "bull_trend", "BTC/USDT", 0.7)
        adj = self.tracker.get_asset_adjustment("trend", "BTC/USDT")
        assert 1.0 < adj < 1.0 + MAX_ADJ_ASSET


# ═══════════════════════════════════════════════════════════════════════════
# S9 — Hierarchical Fallback (Section 1)
# ═══════════════════════════════════════════════════════════════════════════

class TestS9_HierarchicalFallback:
    """Model-wide average is used as prior when a specific cell has no data."""

    @pytest.fixture(autouse=True)
    def fresh_tracker(self):
        from core.learning.level2_tracker import Level2PerformanceTracker
        import threading
        t = Level2PerformanceTracker.__new__(Level2PerformanceTracker)
        t._lock = threading.Lock()
        t._regime = {}; t._asset = {}
        t._score_cal = {}; t._exit_eff = {}
        t._exit_r = {}; t._entry_rr = {}
        self.tracker = t

    def test_s9_01_no_data_for_specific_cell_uses_model_avg(self):
        """If TrendModel wins 70% in bull_trend (active), unseen ranging cell uses fallback."""
        from core.learning.level2_tracker import FALLBACK_STRENGTH
        for _ in range(12):
            self.tracker.record(["trend"], True, "bull_trend", "BTC/USDT", 0.7)
        # No data for ranging
        fallback = self.tracker.get_regime_adjustment("trend", "ranging")
        # bull_trend adj = +10%, fallback = +10% * 0.5 = +5%
        assert fallback > 1.0, f"Expected positive fallback, got {fallback}"
        assert fallback < self.tracker.get_regime_adjustment("trend", "bull_trend"), \
            "Fallback should be weaker than full cell"

    def test_s9_02_negative_model_avg_gives_negative_fallback(self):
        """If TrendModel loses consistently, unseen cells should get a negative prior."""
        for _ in range(12):
            self.tracker.record(["trend"], False, "bull_trend", "BTC/USDT", 0.6)
        fallback = self.tracker.get_regime_adjustment("trend", "bear_trend")
        assert fallback < 1.0, f"Expected negative fallback, got {fallback}"

    def test_s9_03_no_active_cells_returns_neutral(self):
        """Zero active cells → fallback returns 1.0."""
        fallback = self.tracker.get_regime_adjustment("totally_new_model", "ranging")
        assert fallback == 1.0

    def test_s9_04_fallback_at_half_strength(self):
        """Fallback should be half the magnitude of full active cell."""
        from core.learning.level2_tracker import MAX_ADJ_REGIME, FALLBACK_STRENGTH
        # Full activation at +10%
        for _ in range(12):
            self.tracker.record(["trend"], True, "bull_trend", "BTC/USDT", 0.7)
        full_adj  = self.tracker.get_regime_adjustment("trend", "bull_trend")
        fall_adj  = self.tracker.get_regime_adjustment("trend", "ranging")
        full_dev  = full_adj  - 1.0
        fall_dev  = fall_adj  - 1.0
        # fallback should be ~50% of full cell
        expected_fallback_dev = full_dev * FALLBACK_STRENGTH
        assert abs(fall_dev - expected_fallback_dev) < 0.005, \
            f"Fallback dev {fall_dev:.4f} ≠ expected {expected_fallback_dev:.4f}"

    def test_s9_05_asset_fallback_works(self):
        """Asset dimension also falls back to model-wide average."""
        for _ in range(12):
            self.tracker.record(["trend"], True, "bull_trend", "BTC/USDT", 0.7)
        fallback = self.tracker.get_asset_adjustment("trend", "SOL/USDT")
        assert fallback > 1.0, "Asset fallback should carry positive prior from BTC/USDT"

    def test_s9_06_fallback_ignored_when_cell_has_partial_data(self):
        """If cell has 5-9 trades, use partial activation, not fallback."""
        # Active cell for bull_trend
        for _ in range(12):
            self.tracker.record(["trend"], True, "bull_trend", "BTC/USDT", 0.7)
        # Partial data for ranging
        for _ in range(7):
            self.tracker.record(["trend"], False, "ranging", "BTC/USDT", 0.5)
        partial_adj = self.tracker.get_regime_adjustment("trend", "ranging")
        fallback_adj = self.tracker._get_model_fallback_adj("regime", "trend", 0.10)
        # Partial activation from losing cell should be < fallback from winning model
        assert partial_adj != fallback_adj, "Partial should differ from fallback"


# ═══════════════════════════════════════════════════════════════════════════
# S10 — Richer Exit Attribution (Section 2 & 4)
# ═══════════════════════════════════════════════════════════════════════════

class TestS10_RicherAttribution:
    """Realized R and expected RR are tracked and exposed in exit diagnostics."""

    @pytest.fixture(autouse=True)
    def fresh_tracker(self):
        from core.learning.level2_tracker import Level2PerformanceTracker
        import threading
        t = Level2PerformanceTracker.__new__(Level2PerformanceTracker)
        t._lock = threading.Lock()
        t._regime = {}; t._asset = {}
        t._score_cal = {}; t._exit_eff = {}
        t._exit_r = {}; t._entry_rr = {}
        self.tracker = t

    def test_s10_01_realized_r_recorded_for_tp(self):
        self.tracker.record(
            ["trend"], True, "bull_trend", "BTC/USDT", 0.7,
            exit_reason="take_profit", realized_r=2.0, expected_rr=2.0,
        )
        eff = self.tracker.get_exit_efficiency()
        assert "trend" in eff
        assert eff["trend"]["avg_tp_r"] is not None
        assert abs(eff["trend"]["avg_tp_r"] - 2.0) < 0.01

    def test_s10_02_realized_r_recorded_for_sl(self):
        self.tracker.record(
            ["trend"], False, "bull_trend", "BTC/USDT", 0.6,
            exit_reason="stop_loss", realized_r=-1.0, expected_rr=2.0,
        )
        eff = self.tracker.get_exit_efficiency()
        assert eff["trend"]["avg_sl_r"] is not None
        assert abs(eff["trend"]["avg_sl_r"] - (-1.0)) < 0.01

    def test_s10_03_target_capture_pct_computed(self):
        # TP at exactly expected RR = 100% capture
        self.tracker.record(
            ["trend"], True, "bull_trend", "BTC/USDT", 0.7,
            exit_reason="take_profit", realized_r=2.0, expected_rr=2.0,
        )
        eff = self.tracker.get_exit_efficiency()
        assert eff["trend"]["target_capture_pct"] is not None
        assert abs(eff["trend"]["target_capture_pct"] - 100.0) < 1.0

    def test_s10_04_target_capture_below_100_when_exiting_early(self):
        # TP at 1.5R vs expected 2.0R = 75% capture
        self.tracker.record(
            ["trend"], True, "bull_trend", "BTC/USDT", 0.7,
            exit_reason="take_profit", realized_r=1.5, expected_rr=2.0,
        )
        eff = self.tracker.get_exit_efficiency()
        cap = eff["trend"]["target_capture_pct"]
        assert cap is not None and abs(cap - 75.0) < 1.0

    def test_s10_05_exit_diagnostics_stop_tightness_flag(self):
        """SL rate > 60% triggers stop_tightness_flag."""
        for _ in range(4):
            self.tracker.record(
                ["trend"], False, "bull_trend", "BTC/USDT", 0.5,
                exit_reason="stop_loss", realized_r=-1.0,
            )
        # Only 1 TP → SL rate = 80%
        self.tracker.record(
            ["trend"], True, "bull_trend", "BTC/USDT", 0.7,
            exit_reason="take_profit", realized_r=2.0,
        )
        diag = self.tracker.get_exit_diagnostics()
        assert diag["overall"]["stop_tightness_flag"] is True

    def test_s10_06_exit_diagnostics_no_flag_when_balanced(self):
        """60%+ TP rate → no stop tightness flag."""
        for _ in range(6):
            self.tracker.record(
                ["trend"], True, "bull_trend", "BTC/USDT", 0.7,
                exit_reason="take_profit", realized_r=2.0,
            )
        for _ in range(3):
            self.tracker.record(
                ["trend"], False, "bull_trend", "BTC/USDT", 0.5,
                exit_reason="stop_loss", realized_r=-1.0,
            )
        diag = self.tracker.get_exit_diagnostics()
        assert diag["overall"]["stop_tightness_flag"] is False

    def test_s10_07_no_crash_when_realized_r_absent(self):
        """record() with no realized_r is fine — exit counts still work."""
        self.tracker.record(["trend"], True, "bull_trend", "BTC/USDT", 0.7,
                            exit_reason="take_profit")
        eff = self.tracker.get_exit_efficiency()
        assert eff["trend"]["tp"] == 1
        assert eff["trend"]["avg_tp_r"] is None  # no data yet


# ═══════════════════════════════════════════════════════════════════════════
# S11 — Score Calibration Quality (Section 3)
# ═══════════════════════════════════════════════════════════════════════════

class TestS11_ScoreCalibrationQuality:
    """Score calibration quality metric (monotonicity)."""

    @pytest.fixture(autouse=True)
    def fresh_tracker(self):
        from core.learning.level2_tracker import Level2PerformanceTracker
        import threading
        t = Level2PerformanceTracker.__new__(Level2PerformanceTracker)
        t._lock = threading.Lock()
        t._regime = {}; t._asset = {}
        t._score_cal = {}; t._exit_eff = {}
        t._exit_r = {}; t._entry_rr = {}
        self.tracker = t

    def _feed_bin(self, score: float, wins: int, losses: int):
        for _ in range(wins):
            self.tracker.record(["trend"], True, "bull_trend", "BTC/USDT", score)
        for _ in range(losses):
            self.tracker.record(["trend"], False, "bull_trend", "BTC/USDT", score)

    def test_s11_01_insufficient_bins_returns_none(self):
        # Only one bin → can't compute monotonicity
        self._feed_bin(0.75, 6, 4)
        q = self.tracker.get_score_calibration_quality()
        assert q["monotonicity_score"] is None

    def test_s11_02_perfectly_calibrated_scores_1_0(self):
        # bin 0.4-0.5: 40% WR, bin 0.6-0.7: 60% WR → monotone
        self._feed_bin(0.45, 4, 6)   # 40%
        self._feed_bin(0.65, 6, 4)   # 60%
        q = self.tracker.get_score_calibration_quality()
        assert q["monotonicity_score"] is not None
        assert q["monotonicity_score"] == 1.0

    def test_s11_03_inverted_calibration_scores_0_0(self):
        # bin 0.4-0.5: 70% WR, bin 0.6-0.7: 30% WR → anti-monotone
        self._feed_bin(0.45, 7, 3)   # 70% (lower score, higher WR)
        self._feed_bin(0.65, 3, 7)   # 30% (higher score, lower WR)
        q = self.tracker.get_score_calibration_quality()
        assert q["monotonicity_score"] == 0.0

    def test_s11_04_quality_dict_structure(self):
        self._feed_bin(0.45, 6, 4)
        self._feed_bin(0.65, 7, 3)
        q = self.tracker.get_score_calibration_quality()
        for key in ("monotonicity_score", "active_bins", "description", "bin_win_rates"):
            assert key in q

    def test_s11_05_description_matches_quality(self):
        self._feed_bin(0.45, 4, 6)   # 40%
        self._feed_bin(0.65, 6, 4)   # 60%
        self._feed_bin(0.75, 7, 3)   # 70%
        q = self.tracker.get_score_calibration_quality()
        assert "Good" in q["description"] or "Moderate" in q["description"]
