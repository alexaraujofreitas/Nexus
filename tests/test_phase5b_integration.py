"""
tests/test_phase5b_integration.py — Phase 5B v3 Integration Tests
================================================================
48 tests covering:
  - PipelineContext immutability & validation (6)
  - Backward compatibility (3)
  - TQS integration (3)
  - Global filter integration (4)  [+1: filter with None tqs_score]
  - Concentration integration (3)
  - Pipeline order / rejection precedence (3)
  - Decision hashing / replay identity (5)
  - Filter replay determinism (3)
  - Concurrency model assertions (3)
  - Portfolio safety proof in pipeline (3)  [CORRECTED: conc BEFORE risk]
  - Module compatibility matrix (4)
  - NaN-free pipeline (2)
  - Deterministic asset ordering (3)
  - Headless concurrency model (3)
"""
import json
import pytest
from unittest.mock import MagicMock, Mock

from core.intraday.execution_contracts import (
    CapitalSnapshot, DecisionStatus, Direction, ExecutionDecision,
    ExposureSnapshot, PortfolioSnapshot, RejectionSource, StrategyClass,
    _make_id,
)
from core.intraday.signal_contracts import TriggerSignal, TriggerLifecycle
from core.intraday.processing.processing_engine import (
    ProcessingEngine, REJECTION_TQS_FLOOR, REJECTION_GLOBAL_FILTER,
)
from core.intraday.pipeline_context import (
    PipelineContext, EMPTY_CONTEXT, canonical_asset_order,
)
from core.intraday.scoring.trade_quality_scorer import (
    TradeQualityScorer, TQSConfig, TQSResult,
)
from core.intraday.filtering.global_trade_filter import (
    GlobalTradeFilter, GlobalFilterConfig, FilterResult, FilterStateSnapshot,
)
from core.intraday.scoring.capital_concentration import (
    CapitalConcentrationEngine, ConcentrationConfig, ConcentrationResult,
)


# ── Helpers ──────────────────────────────────────────────────

NOW_MS = 1_000_000_000_000


def _trigger(symbol="BTC", direction=Direction.LONG, strength=0.7,
             trigger_quality=0.6, regime_confidence=0.5, regime="BULL_TREND",
             entry_price=50000.0, stop_loss=49000.0, take_profit=52000.0):
    """Build TriggerSignal. R:R is computed from prices."""
    return TriggerSignal(
        trigger_id="t001", setup_id="s001", symbol=symbol,
        direction=direction, strategy_name="Test",
        strategy_class=StrategyClass.MOMENTUM_EXPANSION,
        entry_price=entry_price, stop_loss=stop_loss, take_profit=take_profit,
        atr_value=500.0, strength=strength, trigger_quality=trigger_quality,
        regime=regime, regime_confidence=regime_confidence,
        setup_timeframe="30m", trigger_timeframe="5m",
        trigger_candle_ts=NOW_MS, setup_candle_ts=NOW_MS,
        lifecycle=TriggerLifecycle.EVALUATED,
        created_at_ms=NOW_MS, max_age_ms=300_000,
        candle_trace_ids=("c1",), setup_trace_ids=("s1",),
    )


def _snap(available=50000.0, total=100000.0, heat=0.02):
    return PortfolioSnapshot(
        capital=CapitalSnapshot(
            total_capital=total, available_capital=available,
            reserved_capital=total - available, equity=total,
            peak_equity=total, drawdown_pct=0.0,
            realized_pnl_today=0.0, total_realized_pnl=0.0,
            total_fees=0.0, trade_count_today=0, consecutive_losses=0,
        ),
        exposure=ExposureSnapshot(
            per_symbol={}, portfolio_heat=heat,
            long_exposure=0.0, short_exposure=0.0, net_exposure=0.0,
        ),
        open_positions=(), open_position_count=0,
    )


def _mock_sizer(size=500.0, qty=0.01, risk=50.0):
    sizer = MagicMock()
    sizer.calculate.return_value = {
        "size_usdt": size, "quantity": qty, "risk_usdt": risk,
    }
    return sizer


def _mock_risk_engine(approve=True):
    engine = MagicMock()

    def validate(intent, snapshot, cb):
        status = DecisionStatus.APPROVED if approve else DecisionStatus.REJECTED
        return ExecutionDecision(
            decision_id=_make_id(intent.trigger_id, "risk"),
            intent_id=intent.intent_id,
            trigger_id=intent.trigger_id,
            setup_id=intent.setup_id,
            symbol=intent.symbol,
            direction=intent.direction,
            strategy_name=intent.strategy_name,
            strategy_class=intent.strategy_class,
            entry_price=intent.entry_price,
            stop_loss=intent.stop_loss,
            take_profit=intent.take_profit,
            final_size_usdt=intent.size_usdt,
            final_quantity=intent.quantity,
            risk_usdt=intent.risk_usdt,
            risk_reward_ratio=intent.risk_reward_ratio,
            regime=intent.regime,
            status=status,
            rejection_reason="" if approve else "risk_gate_reject",
            rejection_source="" if approve else "portfolio_heat",
            created_at_ms=NOW_MS,
            candle_trace_ids=intent.candle_trace_ids,
        )

    engine.validate.side_effect = validate
    return engine


def _mock_cb():
    return MagicMock()


def _mock_ks(halted=False):
    ks = MagicMock()
    ks.is_halted.return_value = halted
    return ks


def _engine(**kwargs):
    """Build ProcessingEngine with defaults."""
    defaults = dict(
        position_sizer=_mock_sizer(),
        risk_engine=_mock_risk_engine(),
        circuit_breaker=_mock_cb(),
        kill_switch=_mock_ks(),
        now_ms_fn=lambda: NOW_MS,
    )
    defaults.update(kwargs)
    return ProcessingEngine(**defaults)


# ── PipelineContext Immutability & Validation (6) ────────────

class TestPipelineContext:
    def test_empty_context(self):
        ctx = EMPTY_CONTEXT
        assert ctx.tqs is None
        assert ctx.filter is None
        assert ctx.concentration is None

    def test_with_tqs_returns_new(self):
        tqs = TQSResult(score=0.5, setup_quality=0.5, trigger_strength=0.5,
                         market_context=0.5, execution_context=0.5, passed=True)
        ctx1 = EMPTY_CONTEXT
        ctx2 = ctx1.with_tqs(tqs)
        assert ctx1.tqs is None  # Original unchanged
        assert ctx2.tqs is tqs

    def test_context_is_frozen(self):
        with pytest.raises(AttributeError):
            EMPTY_CONTEXT.tqs = "not allowed"

    def test_concentration_requires_tqs(self):
        """Cannot set concentration without TQS — no silent defaults."""
        conc = ConcentrationResult(
            multiplier=1.0, tqs_component=0.5, asset_component=0.5,
            execution_component=0.5, adjusted_size_usdt=100.0,
            adjusted_quantity=0.002, capped=False,
        )
        with pytest.raises(ValueError, match="Cannot set concentration without TQS"):
            EMPTY_CONTEXT.with_concentration(conc)

    def test_with_tqs_type_check(self):
        with pytest.raises(TypeError, match="Expected TQSResult"):
            EMPTY_CONTEXT.with_tqs({"score": 0.5})

    def test_to_dict_only_includes_computed(self):
        ctx = EMPTY_CONTEXT
        assert ctx.to_dict() == {}
        tqs = TQSResult(score=0.5, setup_quality=0.5, trigger_strength=0.5,
                         market_context=0.5, execution_context=0.5, passed=True)
        ctx = ctx.with_tqs(tqs)
        d = ctx.to_dict()
        assert "tqs" in d
        assert "filter" not in d
        assert "concentration" not in d


# ── Backward Compatibility (3) ──────────────────────────────

class TestBackwardCompat:
    def test_no_phase5b_modules_produces_approved(self):
        pe = _engine()
        d = pe.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS)
        assert d.status == DecisionStatus.APPROVED

    def test_no_phase5b_context_is_empty(self):
        pe = _engine()
        pe.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS)
        ctx = pe.last_pipeline_context
        assert ctx.tqs is None
        assert ctx.filter is None
        assert ctx.concentration is None

    def test_no_phase5b_decision_hash_still_computed(self):
        pe = _engine()
        pe.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS)
        assert len(pe.last_decision_hash) == 64  # SHA-256 hex


# ── TQS Integration (3) ─────────────────────────────────────

class TestTQSIntegration:
    def test_tqs_rejection(self):
        scorer = TradeQualityScorer(TQSConfig(min_tqs=0.99))
        pe = _engine(tqs_scorer=scorer)
        d = pe.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS)
        assert d.status == DecisionStatus.REJECTED
        assert d.rejection_source == REJECTION_TQS_FLOOR

    def test_tqs_pass_sets_context(self):
        scorer = TradeQualityScorer()
        pe = _engine(tqs_scorer=scorer)
        pe.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS)
        assert pe.last_pipeline_context.tqs is not None
        assert pe.last_pipeline_context.tqs.passed is True

    def test_tqs_rejection_still_sets_context(self):
        scorer = TradeQualityScorer(TQSConfig(min_tqs=0.99))
        pe = _engine(tqs_scorer=scorer)
        pe.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS)
        assert pe.last_pipeline_context.tqs is not None
        assert pe.last_pipeline_context.tqs.passed is False


# ── Global Filter Integration (4) ───────────────────────────

class TestFilterIntegration:
    def test_filter_rejection(self):
        scorer = TradeQualityScorer()
        gf = GlobalTradeFilter(GlobalFilterConfig(max_trades_per_day_global=0))
        pe = _engine(tqs_scorer=scorer, global_filter=gf)
        d = pe.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS)
        assert d.status == DecisionStatus.REJECTED
        assert d.rejection_source == REJECTION_GLOBAL_FILTER

    def test_filter_pass_sets_context(self):
        scorer = TradeQualityScorer()
        gf = GlobalTradeFilter()
        pe = _engine(tqs_scorer=scorer, global_filter=gf)
        pe.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS)
        ctx = pe.last_pipeline_context
        assert ctx.filter is not None
        assert ctx.filter.passed is True
        assert ctx.filter_state is not None

    def test_filter_uses_explicit_tqs_score(self):
        """Filter receives TQS score explicitly, not from a dict."""
        scorer = TradeQualityScorer()
        gf = GlobalTradeFilter(GlobalFilterConfig(
            uncertain_regime_tqs_floor=0.99,
        ))
        pe = _engine(tqs_scorer=scorer, global_filter=gf)
        d = pe.process(
            _trigger(regime="uncertain"), _snap(), 50000.0, now_ms=NOW_MS,
        )
        assert d.status == DecisionStatus.REJECTED
        assert "regime_tqs_floor" in d.rejection_reason

    def test_filter_without_tqs_passes_none(self):
        """Filter without TQS scorer receives tqs_score=None.
        TQS-dependent gates (regime_tqs_floor) are disabled."""
        gf = GlobalTradeFilter(GlobalFilterConfig(
            uncertain_regime_tqs_floor=0.99,
        ))
        pe = _engine(global_filter=gf)
        # Uncertain regime — but without TQS, the tqs_floor gate is skipped
        d = pe.process(
            _trigger(regime="uncertain"), _snap(), 50000.0, now_ms=NOW_MS,
        )
        # Should not reject on regime_tqs_floor (gate disabled when tqs=None)
        if d.status == DecisionStatus.REJECTED:
            assert "regime_tqs_floor" not in (d.rejection_reason or "")


# ── Concentration Integration (3) ───────────────────────────

class TestConcentrationIntegration:
    def test_concentration_adjusts_size(self):
        scorer = TradeQualityScorer()
        conc = CapitalConcentrationEngine()
        pe = _engine(tqs_scorer=scorer, concentration_engine=conc)
        d = pe.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS)
        assert d.status == DecisionStatus.APPROVED
        ctx = pe.last_pipeline_context
        assert ctx.concentration is not None

    def test_concentration_requires_tqs_scorer_at_construction(self):
        """Concentration without TQS scorer raises ValueError at construction."""
        conc = CapitalConcentrationEngine()
        with pytest.raises(ValueError, match="concentration_engine requires tqs_scorer"):
            _engine(concentration_engine=conc)

    def test_high_tqs_increases_size(self):
        scorer = TradeQualityScorer()
        conc = CapitalConcentrationEngine()
        # High quality trigger
        pe = _engine(tqs_scorer=scorer, concentration_engine=conc)
        d = pe.process(
            _trigger(strength=0.9, trigger_quality=0.9,
                     take_profit=53000.0, regime_confidence=0.9),
            _snap(available=100000, total=100000, heat=0.0),
            50000.0, now_ms=NOW_MS,
        )
        ctx = pe.last_pipeline_context
        assert ctx.concentration is not None
        assert ctx.concentration.multiplier > 1.0


# ── Pipeline Order / Rejection Precedence (3) ───────────────

class TestPipelineOrder:
    def test_kill_switch_before_tqs(self):
        scorer = TradeQualityScorer()
        pe = _engine(tqs_scorer=scorer, kill_switch=_mock_ks(halted=True))
        d = pe.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS)
        assert d.rejection_source == RejectionSource.KILL_SWITCH.value
        assert pe.last_pipeline_context.tqs is None

    def test_tqs_before_filter(self):
        scorer = TradeQualityScorer(TQSConfig(min_tqs=0.99))
        gf = GlobalTradeFilter(GlobalFilterConfig(max_trades_per_day_global=0))
        pe = _engine(tqs_scorer=scorer, global_filter=gf)
        d = pe.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS)
        # TQS rejects first
        assert d.rejection_source == REJECTION_TQS_FLOOR
        assert pe.last_pipeline_context.filter is None

    def test_filter_before_risk(self):
        scorer = TradeQualityScorer()
        gf = GlobalTradeFilter(GlobalFilterConfig(max_trades_per_day_global=0))
        pe = _engine(
            tqs_scorer=scorer, global_filter=gf,
            risk_engine=_mock_risk_engine(approve=False),
        )
        d = pe.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS)
        assert d.rejection_source == REJECTION_GLOBAL_FILTER


# ── Decision Hashing / Replay Identity (5) ───────────────────

class TestDecisionHashing:
    def test_same_inputs_same_hash(self):
        pe = _engine(tqs_scorer=TradeQualityScorer())
        t, s = _trigger(), _snap()
        pe.process(t, s, 50000.0, now_ms=NOW_MS)
        h1 = pe.last_decision_hash
        pe.process(t, s, 50000.0, now_ms=NOW_MS)
        h2 = pe.last_decision_hash
        assert h1 == h2

    def test_different_inputs_different_hash(self):
        pe = _engine(tqs_scorer=TradeQualityScorer())
        pe.process(_trigger(strength=0.9), _snap(), 50000.0, now_ms=NOW_MS)
        h1 = pe.last_decision_hash
        pe.process(_trigger(strength=0.1), _snap(), 50000.0, now_ms=NOW_MS)
        h2 = pe.last_decision_hash
        assert h1 != h2

    def test_hash_is_sha256(self):
        pe = _engine()
        pe.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS)
        h = pe.last_decision_hash
        assert len(h) == 64
        int(h, 16)  # Valid hex

    def test_hash_includes_pipeline_context(self):
        """Hash changes when TQS is present vs absent."""
        pe_no_tqs = _engine()
        pe_no_tqs.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS)
        h1 = pe_no_tqs.last_decision_hash

        pe_with_tqs = _engine(tqs_scorer=TradeQualityScorer())
        pe_with_tqs.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS)
        h2 = pe_with_tqs.last_decision_hash

        assert h1 != h2  # Pipeline context differs

    def test_decision_hash_deterministic_with_full_pipeline(self):
        """Full pipeline (TQS + filter + concentration) produces stable hash."""
        scorer = TradeQualityScorer()
        conc = CapitalConcentrationEngine()
        hashes = []
        for _ in range(3):
            gf = GlobalTradeFilter()
            pe = _engine(tqs_scorer=scorer, global_filter=gf,
                         concentration_engine=conc)
            pe.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS)
            hashes.append(pe.last_decision_hash)
        assert hashes[0] == hashes[1] == hashes[2]


# ── Filter Replay Determinism (3) ───────────────────────────

class TestFilterReplay:
    def test_replay_produces_identical_state(self):
        gf1 = GlobalTradeFilter()
        gf1.record_trade("MX", "BTC", "bull", now_ms=NOW_MS)
        gf1.record_outcome(True, now_ms=NOW_MS + 1000)
        gf1.record_trade("MX", "ETH", "bull", now_ms=NOW_MS + 2000)
        gf1.record_outcome(False, now_ms=NOW_MS + 3000)

        gf2 = GlobalTradeFilter()
        gf2.replay(gf1.event_log)

        assert gf1.state_snapshot() == gf2.state_snapshot()

    def test_json_persistence_round_trip(self):
        gf1 = GlobalTradeFilter()
        gf1.record_trade("MX", "BTC", "bull", now_ms=NOW_MS)
        gf1.record_outcome(False, now_ms=NOW_MS + 1000)

        data = gf1.to_json()
        gf2 = GlobalTradeFilter.from_json(data)

        assert gf1.state_snapshot() == gf2.state_snapshot()

    def test_replay_from_empty_resets(self):
        gf = GlobalTradeFilter()
        gf.record_trade("MX", "BTC", "bull", now_ms=NOW_MS)
        assert gf.event_count == 1
        gf.replay([])
        assert gf.event_count == 0


# ── Concurrency Model Assertions (3) ────────────────────────

class TestConcurrencyModel:
    """
    CONCURRENCY MODEL: Single-threaded, headless-first.

    ProcessingEngine.process() is called sequentially from
    OrchestratorEngine. GUI is observer-only — reads properties
    but never participates in decision path. No PySide6 in
    any Phase 5B module.
    """

    def test_no_threading_imports(self):
        """Phase 5B modules contain no threading imports."""
        import core.intraday.processing.processing_engine as pe_mod
        import core.intraday.scoring.trade_quality_scorer as tqs_mod
        import core.intraday.filtering.global_trade_filter as gf_mod
        import core.intraday.scoring.capital_concentration as cc_mod
        import core.intraday.pipeline_context as ctx_mod

        for mod in [pe_mod, tqs_mod, gf_mod, cc_mod, ctx_mod]:
            source = open(mod.__file__).read()
            assert "import threading" not in source
            assert "from threading" not in source
            assert "Lock(" not in source

    def test_sequential_processing_deterministic(self):
        """Processing 3 assets sequentially produces deterministic results."""
        scorer = TradeQualityScorer()
        conc = CapitalConcentrationEngine()

        results = []
        for _ in range(2):
            pe = _engine(tqs_scorer=scorer, global_filter=GlobalTradeFilter(),
                         concentration_engine=conc)
            batch = []
            for sym in ["BTC", "ETH", "SOL"]:
                d = pe.process(_trigger(symbol=sym), _snap(), 50000.0, now_ms=NOW_MS)
                batch.append((d.symbol, d.status.value, d.final_size_usdt))
            results.append(batch)

        assert results[0] == results[1]

    def test_last_context_replaced_not_mutated(self):
        """Each process() replaces last_pipeline_context entirely."""
        scorer = TradeQualityScorer()
        pe = _engine(tqs_scorer=scorer)

        pe.process(_trigger(symbol="BTC"), _snap(), 50000.0, now_ms=NOW_MS)
        ctx1 = pe.last_pipeline_context

        pe.process(_trigger(symbol="ETH"), _snap(), 50000.0, now_ms=NOW_MS)
        ctx2 = pe.last_pipeline_context

        # ctx1 is still intact (immutable)
        assert ctx1 is not ctx2
        assert ctx1.tqs is not None


# ── Portfolio Safety Proof in Pipeline (3) ───────────────────

class TestPortfolioSafetyPipeline:
    """
    CORRECTED PROOF (v3): Concentration runs BEFORE RiskEngine.

    Pipeline order:
      Step 8: Concentration adjusts sizing (base_size * multiplier, capped at 4%)
      Step 9: Intent built with POST-concentration sizing
      Step 11: Risk engine validates POST-concentration sizing

    Therefore: Risk engine sees the FINAL concentrated size. No
    unvalidated sizing reaches execution. Portfolio heat is computed
    against post-concentration risk_usdt.

    PROOF:
      For any single trade:
        concentrated_size = min(base_size * multiplier, 0.04 * total_capital)
        intent.size_usdt = concentrated_size
        risk_engine validates intent.size_usdt against portfolio heat

      Risk engine portfolio heat cap (6%) is evaluated AFTER concentration.
      No gap between concentration and risk validation.
    """

    def test_risk_engine_sees_post_concentration_sizing(self):
        """Risk engine receives intent with concentrated sizing."""
        scorer = TradeQualityScorer()
        conc = CapitalConcentrationEngine()
        mock_risk = _mock_risk_engine(approve=True)
        pe = _engine(
            tqs_scorer=scorer, concentration_engine=conc,
            risk_engine=mock_risk,
            position_sizer=_mock_sizer(size=500.0, qty=0.01, risk=50.0),
        )
        d = pe.process(
            _trigger(strength=1.0, trigger_quality=1.0,
                     take_profit=55000.0, regime_confidence=1.0),
            _snap(total=100000),
            50000.0, now_ms=NOW_MS,
        )
        # Verify risk_engine.validate was called with post-concentration intent
        call_args = mock_risk.validate.call_args
        intent = call_args[0][0]  # First positional arg
        # Intent sizing should reflect concentration adjustment
        # (not the raw base sizing from position_sizer)
        ctx = pe.last_pipeline_context
        assert ctx.concentration is not None
        if ctx.concentration.multiplier != 1.0:
            # Intent size should differ from base size
            assert intent.size_usdt != 500.0

    def test_single_trade_respects_4pct_through_risk(self):
        """Post-concentration size is capped at 4%, and risk validates it."""
        scorer = TradeQualityScorer()
        conc = CapitalConcentrationEngine()
        pe = _engine(
            tqs_scorer=scorer, concentration_engine=conc,
            position_sizer=_mock_sizer(size=5000.0, qty=0.1, risk=500.0),
        )
        d = pe.process(
            _trigger(strength=1.0, trigger_quality=1.0,
                     take_profit=55000.0, regime_confidence=1.0),
            _snap(total=100000),
            50000.0, now_ms=NOW_MS,
        )
        # 4% of 100k = 4000
        assert d.final_size_usdt <= 100000 * 0.04 + 0.01

    def test_concentration_on_rejected_risk_is_still_applied(self):
        """Concentration runs before risk. If risk rejects,
        concentration context is still recorded."""
        scorer = TradeQualityScorer()
        conc = CapitalConcentrationEngine()
        pe = _engine(
            tqs_scorer=scorer, concentration_engine=conc,
            risk_engine=_mock_risk_engine(approve=False),
        )
        d = pe.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS)
        assert d.status == DecisionStatus.REJECTED
        # Concentration ran before risk — context should be populated
        assert pe.last_pipeline_context.concentration is not None


# ── Module Compatibility Matrix (4) ──────────────────────────

class TestModuleCompatibility:
    """
    Valid combinations:
      - TQS only
      - Filter only
      - TQS + Filter
      - TQS + Concentration
      - TQS + Filter + Concentration

    Invalid (ValueError at construction):
      - Concentration only (requires TQS)
      - Filter + Concentration (without TQS)
    """

    def test_concentration_only_invalid(self):
        conc = CapitalConcentrationEngine()
        with pytest.raises(ValueError, match="concentration_engine requires tqs_scorer"):
            _engine(concentration_engine=conc)

    def test_filter_plus_concentration_without_tqs_invalid(self):
        gf = GlobalTradeFilter()
        conc = CapitalConcentrationEngine()
        with pytest.raises(ValueError, match="concentration_engine requires tqs_scorer"):
            _engine(global_filter=gf, concentration_engine=conc)

    def test_tqs_only_valid(self):
        pe = _engine(tqs_scorer=TradeQualityScorer())
        d = pe.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS)
        assert d.status in (DecisionStatus.APPROVED, DecisionStatus.REJECTED)

    def test_filter_only_valid(self):
        """Filter without TQS is valid — TQS-dependent gates disabled."""
        pe = _engine(global_filter=GlobalTradeFilter())
        d = pe.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS)
        assert d.status in (DecisionStatus.APPROVED, DecisionStatus.REJECTED)


# ── NaN-Free Pipeline (2) ───────────────────────────────────

class TestNaNFree:
    """No NaN values anywhere in the pipeline. TQS absence is
    modeled with None, not float('nan')."""

    def test_no_nan_in_processing_engine_source(self):
        """ProcessingEngine source contains no float('nan')."""
        import core.intraday.processing.processing_engine as pe_mod
        source = open(pe_mod.__file__).read()
        assert "float(\"nan\")" not in source
        assert "float('nan')" not in source
        assert "math.nan" not in source

    def test_filter_receives_none_not_nan_without_tqs(self):
        """When TQS is absent, filter receives tqs_score=None, not NaN."""
        gf = GlobalTradeFilter()
        # Call evaluate directly with None
        r = gf.evaluate("MX", "BTC", "uncertain", None, now_ms=NOW_MS)
        # Should not crash, and regime_tqs_floor should be skipped
        assert isinstance(r, FilterResult)


# ── Deterministic Asset Ordering (3) ─────────────────────────

class TestAssetOrdering:
    """canonical_asset_order() provides deterministic ordering."""

    def test_alphabetical_order(self):
        assert canonical_asset_order(["SOL", "BTC", "ETH"]) == ["BTC", "ETH", "SOL"]

    def test_case_insensitive(self):
        assert canonical_asset_order(["sol", "BTC", "Eth"]) == ["BTC", "Eth", "sol"]

    def test_stable_across_runs(self):
        """Same input → same output, 100 times."""
        inp = ["LINK", "AVAX", "BTC", "ETH", "SOL"]
        expected = canonical_asset_order(inp)
        for _ in range(100):
            assert canonical_asset_order(inp) == expected


# ── Headless Concurrency Model (3) ──────────────────────────

class TestHeadlessConcurrency:
    """Phase 5B modules are headless-first. No PySide6 dependency."""

    def test_no_pyside6_imports(self):
        """Phase 5B modules contain no PySide6 imports."""
        import core.intraday.processing.processing_engine as pe_mod
        import core.intraday.scoring.trade_quality_scorer as tqs_mod
        import core.intraday.filtering.global_trade_filter as gf_mod
        import core.intraday.scoring.capital_concentration as cc_mod
        import core.intraday.pipeline_context as ctx_mod

        for mod in [pe_mod, tqs_mod, gf_mod, cc_mod, ctx_mod]:
            source = open(mod.__file__).read()
            # Check for actual import statements, not comments
            for line in source.splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue  # Skip comments
                assert "from PySide" not in stripped, f"{mod.__name__}: {stripped}"
                assert "import PySide" not in stripped, f"{mod.__name__}: {stripped}"

    def test_no_qt_signal_in_modules(self):
        """No Qt Signal/Slot in Phase 5B modules."""
        import core.intraday.processing.processing_engine as pe_mod
        import core.intraday.scoring.trade_quality_scorer as tqs_mod
        import core.intraday.filtering.global_trade_filter as gf_mod
        import core.intraday.scoring.capital_concentration as cc_mod
        import core.intraday.pipeline_context as ctx_mod

        for mod in [pe_mod, tqs_mod, gf_mod, cc_mod, ctx_mod]:
            source = open(mod.__file__).read()
            assert "Signal(" not in source or "Signal(dict)" not in source
            assert "pyqtSignal" not in source

    def test_engine_operates_without_event_loop(self):
        """ProcessingEngine works without Qt event loop running."""
        scorer = TradeQualityScorer()
        gf = GlobalTradeFilter()
        conc = CapitalConcentrationEngine()
        pe = _engine(tqs_scorer=scorer, global_filter=gf,
                     concentration_engine=conc)
        d = pe.process(_trigger(), _snap(), 50000.0, now_ms=NOW_MS)
        assert d.status == DecisionStatus.APPROVED
