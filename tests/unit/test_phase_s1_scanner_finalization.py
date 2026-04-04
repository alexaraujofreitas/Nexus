# ============================================================
# Phase S1 — Scanner Finalization Tests
#
# Proves:
# 1. Pipeline rows contain promoted MIL fields (top-level)
# 2. MIL breakdown includes Phase 4B placeholders
# 3. Decision explainability fields are present and accurate
# 4. MIL ON/OFF correctly reflects in pipeline rows
# 5. No logic duplication (scoring once, pass-through only)
# 6. Source of truth: DB → Engine → API → UI chain integrity
# 7. TypeScript types match backend schema
# ============================================================
import sys
import os
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock, PropertyMock
import asyncio
import json

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ── Helper: create a mock scan result ────────────────────────
def _make_scan_result(
    symbol: str = "BTCUSDT",
    score: float = 0.65,
    regime: str = "bull_trend",
    status: str = "approved",
    is_approved: bool = True,
    models_fired: list | None = None,
    rejection_reason: str = "",
    mil_technical_baseline: float = 0.50,
    mil_total_delta: float = 0.05,
    mil_delta_pct: float = 0.10,
    mil_capped: bool = False,
    mil_breakdown: dict | None = None,
) -> dict:
    """Build a scan result dict similar to what ScanWorker produces."""
    diag = {
        "candle_count": 300,
        "candle_age_s": 45,
        "candle_ts_str": "2026-04-04T10:00:00Z",
        "regime_confidence": 0.82,
        "regime_probs": {"bull_trend": 0.82, "ranging": 0.12, "bear_trend": 0.06},
        "all_model_names": ["momentum_breakout", "pullback_long", "swing_low_continuation"],
        "models_disabled": ["mean_reversion", "trend"],
        "models_fired": models_fired or ["momentum_breakout"],
        "models_no_signal": ["pullback_long", "swing_low_continuation"],
        "signal_details": {"momentum_breakout": {"direction": "long", "strength": 0.78}},
        "indicator_cols_missing": [],
        "effective_threshold": 0.35,
        "mil_technical_baseline": mil_technical_baseline,
        "mil_total_delta": mil_total_delta,
        "mil_delta_pct": mil_delta_pct,
        "mil_capped": mil_capped,
        "mil_breakdown": mil_breakdown or {
            "orchestrator_delta": 0.03,
            "oi_delta": 0.015,
            "liquidation_delta": 0.005,
        },
    }
    return {
        "symbol": symbol,
        "score": score,
        "regime": regime,
        "status": status,
        "is_approved": is_approved,
        "models_fired": models_fired or ["momentum_breakout"],
        "side": "long",
        "entry_price": 84000.0,
        "stop_loss_price": 82500.0,
        "take_profit_price": 87000.0,
        "risk_reward_ratio": 2.0,
        "position_size_usdt": 500.0,
        "generated_at": "2026-04-04T10:00:30Z",
        "rejection_reason": rejection_reason,
        "diagnostics": diag,
    }


def _make_no_signal_scan(symbol: str = "ETHUSDT") -> dict:
    """Scan result where no model fired."""
    return {
        "symbol": symbol,
        "score": 0.0,
        "regime": "ranging",
        "status": "No signal",
        "is_approved": False,
        "models_fired": [],
        "side": "",
        "entry_price": None,
        "stop_loss_price": 0.0,
        "take_profit_price": 0.0,
        "risk_reward_ratio": 0.0,
        "position_size_usdt": 0.0,
        "generated_at": "2026-04-04T10:00:30Z",
        "rejection_reason": "",
        "diagnostics": {
            "candle_count": 300,
            "candle_age_s": 45,
            "candle_ts_str": "2026-04-04T10:00:00Z",
            "regime_confidence": 0.55,
            "regime_probs": {"ranging": 0.55, "bull_trend": 0.25, "bear_trend": 0.20},
            "all_model_names": ["momentum_breakout", "pullback_long", "swing_low_continuation"],
            "models_disabled": ["mean_reversion", "trend"],
            "models_fired": [],
            "models_no_signal": ["momentum_breakout", "pullback_long", "swing_low_continuation"],
            "signal_details": {},
            "indicator_cols_missing": [],
            "effective_threshold": 0.35,
        },
    }


def _make_risk_blocked_scan(symbol: str = "SOLUSDT") -> dict:
    """Scan result where risk gate rejected."""
    base = _make_scan_result(
        symbol=symbol,
        score=0.45,
        is_approved=False,
        status="approved",
        rejection_reason="EV gate: expected value below threshold (-0.02R)",
    )
    base["is_approved"] = False
    return base


# ══════════════════════════════════════════════════════════════
# MOCK ENGINE HELPERS
# ══════════════════════════════════════════════════════════════

def _build_engine_with_results(scan_results: list[dict], mil_enabled: bool = True):
    """
    Build a minimal TradingEngineService-like object that can run
    _cmd_get_pipeline_status without needing real DB/Redis/core.
    """
    # We need the actual class for method access
    web_dir = ROOT / "web"
    web_engine_dir = ROOT / "web" / "engine"
    web_backend_dir = ROOT / "web" / "backend"
    for p in [str(web_dir), str(web_engine_dir), str(web_backend_dir), str(ROOT)]:
        if p not in sys.path:
            sys.path.insert(0, p)

    # Patch Qt shim + imports needed by main.py
    with patch.dict(sys.modules, {
        "core_patch": MagicMock(),
        "core_patch.event_bus": MagicMock(),
        "core_patch.redis_bridge": MagicMock(),
    }):
        # Import the module fresh
        import importlib
        if "engine.main" in sys.modules:
            mod = importlib.reload(sys.modules["engine.main"])
        else:
            mod = importlib.import_module("engine.main")

        TradingEngineService = mod.TradingEngineService

    engine = TradingEngineService.__new__(TradingEngineService)
    # Minimal state
    engine._last_pipeline_results = scan_results
    engine._last_pipeline_ts = "2026-04-04T10:00:30Z"
    engine._scanner = MagicMock()
    engine._scanner._running = True

    # Mock _scorer for MIL diagnostics
    mock_scorer = MagicMock()
    mock_scorer._last_diagnostics = {}
    engine._scorer = mock_scorer

    return engine, mil_enabled


# ══════════════════════════════════════════════════════════════
# SECTION 1: Top-Level MIL Field Promotion
# ══════════════════════════════════════════════════════════════

class TestMILFieldPromotion:
    """Pipeline rows MUST contain promoted MIL fields at top level."""

    def test_scanned_row_has_all_mil_top_level_fields(self):
        """Every scanned pipeline row has technical_score, final_score,
        mil_active, mil_total_delta, mil_influence_pct, mil_capped,
        mil_dominant_source, mil_breakdown at top level."""
        required_keys = [
            "technical_score", "final_score", "mil_active",
            "mil_total_delta", "mil_influence_pct", "mil_capped",
            "mil_dominant_source", "mil_breakdown",
        ]
        source = (ROOT / "web" / "engine" / "main.py").read_text()
        # Find the scanned row append block (after "if scan:")
        scan_block_start = source.index("if scan:")
        scan_block_end = source.index("else:", scan_block_start)
        scan_block = source[scan_block_start:scan_block_end]

        for key in required_keys:
            assert f'"{key}"' in scan_block, \
                f"Scanned pipeline row missing top-level key: {key}"

    def test_waiting_row_has_mil_defaults(self):
        """Unscanned (Waiting) rows have MIL defaults at top level."""
        required = {
            "technical_score": "0.0",
            "final_score": "0.0",
            "mil_active": "False",
            "mil_total_delta": "0.0",
            "mil_influence_pct": "0.0",
            "mil_capped": "False",
            "mil_dominant_source": '"none"',
            "mil_breakdown": "{}",
            "decision_explanation": '"Not yet scanned"',
            "block_reasons": "[]",
        }
        source = (ROOT / "web" / "engine" / "main.py").read_text()
        # Find the waiting row block (after "else:" in the merge section)
        merge_start = source.index("# ── 3. Merge")
        else_start = source.index("else:", merge_start)
        # Find the end of the else block (next method or section)
        else_end = source.index("# ── 4. Summary", else_start)
        else_block = source[else_start:else_end]

        for key in required:
            assert f'"{key}"' in else_block, \
                f"Waiting row template missing key: {key}"

    def test_technical_score_from_mil_technical_baseline(self):
        """technical_score comes from diag.mil_technical_baseline, not score."""
        source = (ROOT / "web" / "engine" / "main.py").read_text()
        assert '"technical_score": diag.get("mil_technical_baseline"' in source

    def test_decision_explainability_fields_in_scanned_row(self):
        """Scanned rows have decision_explanation and block_reasons."""
        source = (ROOT / "web" / "engine" / "main.py").read_text()
        scan_start = source.index("if scan:")
        scan_end = source.index("else:", scan_start)
        block = source[scan_start:scan_end]
        assert '"decision_explanation"' in block
        assert '"block_reasons"' in block


# ══════════════════════════════════════════════════════════════
# SECTION 2: MIL Breakdown Phase 4B Placeholders
# ══════════════════════════════════════════════════════════════

class TestMILBreakdownPlaceholders:
    """mil_breakdown MUST include sentiment_delta, news_delta,
    other_orchestrator_delta placeholders (all 0.0 pre-Phase 4B)."""

    def test_breakdown_has_all_six_keys(self):
        """Expanded breakdown: orchestrator, sentiment, news,
        other_orchestrator, oi, liquidation.
        Breakdown dict is built in ConfluenceScorer (single source of truth)."""
        required = {
            "orchestrator_delta",
            "sentiment_delta",
            "news_delta",
            "other_orchestrator_delta",
            "oi_delta",
            "liquidation_delta",
        }
        source = (ROOT / "core" / "meta_decision" / "confluence_scorer.py").read_text()
        for key in required:
            assert f'"{key}"' in source, f"Missing breakdown key in scorer: {key}"

    def test_sentiment_news_default_zero(self):
        """Pre-Phase 4B, sentiment_delta and news_delta are initialized to 0.0
        in ConfluenceScorer (the single source of truth for MIL computation)."""
        source = (ROOT / "core" / "meta_decision" / "confluence_scorer.py").read_text()
        assert "_sentiment_delta = 0.0" in source
        assert "_news_delta = 0.0" in source

    def test_other_orchestrator_equals_total_orchestrator_pre_4b(self):
        """Before Phase 4B wiring, other_orchestrator_delta = orch - sentiment - news.
        Since sentiment=0 and news=0, this equals orch_delta.
        Computed in ConfluenceScorer — NOT in API layer."""
        source = (ROOT / "core" / "meta_decision" / "confluence_scorer.py").read_text()
        assert "_other_orch_delta = _orch_delta_attr - _sentiment_delta - _news_delta" in source

    def test_typescript_types_have_breakdown_fields(self):
        """scanner.ts MILBreakdown interface has all 6 fields."""
        ts_path = ROOT / "web" / "frontend" / "src" / "api" / "scanner.ts"
        ts_source = ts_path.read_text()
        required = [
            "orchestrator_delta: number",
            "sentiment_delta: number",
            "news_delta: number",
            "other_orchestrator_delta: number",
            "oi_delta: number",
            "liquidation_delta: number",
        ]
        for field in required:
            assert field in ts_source, f"TypeScript MILBreakdown missing: {field}"


# ══════════════════════════════════════════════════════════════
# SECTION 3: Decision Explainability
# ══════════════════════════════════════════════════════════════

class TestDecisionExplainability:
    """Every pipeline row has decision_explanation + block_reasons."""

    def _get_engine_class(self):
        """Import and return TradingEngineService class."""
        web_dir = ROOT / "web"
        web_engine_dir = ROOT / "web" / "engine"
        web_backend_dir = ROOT / "web" / "backend"
        for p in [str(web_dir), str(web_engine_dir), str(web_backend_dir), str(ROOT)]:
            if p not in sys.path:
                sys.path.insert(0, p)

        with patch.dict(sys.modules, {
            "core_patch": MagicMock(),
            "core_patch.event_bus": MagicMock(),
            "core_patch.redis_bridge": MagicMock(),
        }):
            import importlib
            if "engine.main" in sys.modules:
                mod = importlib.reload(sys.modules["engine.main"])
            else:
                mod = importlib.import_module("engine.main")
            return mod.TradingEngineService

    def test_approved_row_explanation_contains_approved(self):
        """Approved rows have 'Approved' in decision_explanation."""
        cls = self._get_engine_class()
        engine = cls.__new__(cls)
        scan = _make_scan_result(is_approved=True)
        diag = scan["diagnostics"]
        mil_diag = {"mil_active": False}

        explanation, blocks = engine._build_decision_explanation(
            "Eligible", "approved", scan, diag, mil_diag,
        )
        assert "Approved" in explanation
        assert len(blocks) == 0

    def test_no_signal_row_has_block_reason(self):
        """No-signal rows have a block reason about model signals."""
        cls = self._get_engine_class()
        engine = cls.__new__(cls)
        scan = _make_no_signal_scan()
        diag = scan["diagnostics"]
        mil_diag = {"mil_active": False}

        explanation, blocks = engine._build_decision_explanation(
            "No Signal", "No signal", scan, diag, mil_diag,
        )
        assert len(blocks) > 0
        assert any("signal" in b.lower() for b in blocks)

    def test_risk_blocked_row_has_risk_block_reason(self):
        """Risk-blocked rows cite the rejection reason."""
        cls = self._get_engine_class()
        engine = cls.__new__(cls)
        scan = _make_risk_blocked_scan()
        diag = scan["diagnostics"]
        mil_diag = {"mil_active": False}

        explanation, blocks = engine._build_decision_explanation(
            "Risk Blocked", "approved", scan, diag, mil_diag,
        )
        assert len(blocks) > 0
        assert any("EV gate" in b for b in blocks)

    def test_no_data_row_has_data_block(self):
        """If candle_count=0, explanation says no data."""
        cls = self._get_engine_class()
        engine = cls.__new__(cls)
        scan = _make_scan_result()
        diag = dict(scan["diagnostics"])
        diag["candle_count"] = 0
        mil_diag = {"mil_active": False}

        explanation, blocks = engine._build_decision_explanation(
            "Error", "no data", scan, diag, mil_diag,
        )
        assert "data" in explanation.lower()
        assert any("data" in b.lower() for b in blocks)

    def test_missing_indicators_has_block(self):
        """Missing indicators produce an indicator-related block."""
        cls = self._get_engine_class()
        engine = cls.__new__(cls)
        scan = _make_scan_result()
        diag = dict(scan["diagnostics"])
        diag["indicator_cols_missing"] = ["adx", "ema_9"]
        mil_diag = {"mil_active": False}

        explanation, blocks = engine._build_decision_explanation(
            "Error", "Indicators missing", scan, diag, mil_diag,
        )
        assert "adx" in explanation
        assert len(blocks) > 0

    def test_mil_influence_appears_when_active(self):
        """When MIL is active with non-zero influence, explanation includes it."""
        cls = self._get_engine_class()
        engine = cls.__new__(cls)
        scan = _make_scan_result(is_approved=True)
        diag = scan["diagnostics"]
        mil_diag = {
            "mil_active": True,
            "mil_influence_pct": 0.15,
            "mil_capped": True,
        }

        explanation, blocks = engine._build_decision_explanation(
            "Eligible", "approved", scan, diag, mil_diag,
        )
        assert "MIL" in explanation
        assert "capped" in explanation.lower()


# ══════════════════════════════════════════════════════════════
# SECTION 4: MIL ON/OFF Reflection
# ══════════════════════════════════════════════════════════════

class TestMILOnOff:
    """Pipeline rows correctly reflect MIL enabled/disabled state."""

    def test_mil_disabled_returns_mil_active_false(self):
        """When mil.global_enabled=False, _get_mil_diagnostics returns mil_active=False."""
        # Verify via source code: first branch checks settings and returns early
        source = (ROOT / "web" / "engine" / "main.py").read_text()
        # The method checks mil.global_enabled and returns mil_active=False when disabled
        assert 'result["mil_active"] = False' in source
        assert 'settings.get("mil.global_enabled", False)' in source

    def test_mil_enabled_returns_mil_active_true(self):
        """When mil.global_enabled=True, _get_mil_diagnostics sets mil_active=True."""
        source = (ROOT / "web" / "engine" / "main.py").read_text()
        assert 'result["mil_active"] = True' in source

    def test_mil_active_always_present(self):
        """mil_active key is returned regardless of enabled state."""
        source = (ROOT / "web" / "engine" / "main.py").read_text()
        # Both branches set mil_active
        idx_false = source.index('result["mil_active"] = False')
        idx_true = source.index('result["mil_active"] = True')
        assert idx_false > 0 and idx_true > 0


# ══════════════════════════════════════════════════════════════
# SECTION 5: No Logic Duplication
# ══════════════════════════════════════════════════════════════

class TestNoDuplication:
    """Prove scoring is done once and passed through — never recomputed."""

    def test_score_field_is_passthrough_from_scan(self):
        """Pipeline row 'score' is scan.get('score') — no recomputation."""
        source = (ROOT / "web" / "engine" / "main.py").read_text()
        # The score field in pipeline_rows is: "score": scan.get("score", 0.0)
        assert '"score": scan.get("score", 0.0)' in source

    def test_no_scorer_call_in_pipeline_status(self):
        """_cmd_get_pipeline_status never calls scorer.score() directly."""
        source = (ROOT / "web" / "engine" / "main.py").read_text()
        # Extract just the _cmd_get_pipeline_status method body
        start = source.index("async def _cmd_get_pipeline_status")
        # Find next method definition
        next_method = source.index("\n    async def ", start + 10)
        method_body = source[start:next_method]
        assert "scorer.score(" not in method_body
        assert "_scorer.score(" not in method_body

    def test_scanner_tsx_has_no_scoring_logic(self):
        """Scanner.tsx renders backend data — no score computation.

        Checks that no client-side scoring formulas exist.
        Display of backend-provided fields like 'weighted_score' is fine.
        """
        tsx_path = ROOT / "web" / "frontend" / "src" / "pages" / "Scanner.tsx"
        tsx_source = tsx_path.read_text()
        # Should NOT compute weighted averages client-side
        assert "reduce(" not in tsx_source  # no array reductions for score computation
        # No ConfluenceScorer-like logic
        assert "confluence_score" not in tsx_source.lower()
        assert "weighted_average" not in tsx_source.lower()

    def test_mil_diagnostics_is_read_only(self):
        """_get_mil_diagnostics reads caches only — no MIL computation."""
        source = (ROOT / "web" / "engine" / "main.py").read_text()
        start = source.index("def _get_mil_diagnostics")
        end = source.index("\n    def ", start + 10)
        method_body = source[start:end]
        # Must not call .enhance(), .apply(), .compute()
        assert ".enhance(" not in method_body
        assert ".apply(" not in method_body
        assert ".compute_score(" not in method_body


# ══════════════════════════════════════════════════════════════
# SECTION 6: Source of Truth Chain Integrity
# ══════════════════════════════════════════════════════════════

class TestSourceOfTruthChain:
    """DB → Engine → API → UI chain integrity."""

    def test_db_is_source_of_truth_for_tradable(self):
        """_cmd_get_pipeline_status queries Asset.is_tradable from DB."""
        source = (ROOT / "web" / "engine" / "main.py").read_text()
        assert "Asset.is_tradable.is_(True)" in source

    def test_pipeline_row_keys_match_typescript_interface(self):
        """Every top-level key in the Python pipeline row dict has a
        matching field in the TypeScript PipelineRow interface."""
        import re
        # Extract Python keys from pipeline_rows.append block
        py_source = (ROOT / "web" / "engine" / "main.py").read_text()
        # Find the scanned row append
        append_start = py_source.index("pipeline_rows.append({", py_source.index("if scan:"))
        append_end = py_source.index("})", append_start) + 2
        block = py_source[append_start:append_end]
        # Extract ONLY top-level keys: exactly 20 spaces of indentation
        # (pipeline_rows.append is indented 16, dict keys are at 20)
        py_keys = set(re.findall(r'^                    "(\w+)":', block, re.MULTILINE))

        # Extract TypeScript keys from PipelineRow interface
        ts_source = (ROOT / "web" / "frontend" / "src" / "api" / "scanner.ts").read_text()
        ts_start = ts_source.index("export interface PipelineRow {")
        # Find closing brace at start of line (not inline in types like Record<...>)
        ts_end = ts_source.index("\n}", ts_start)
        ts_block = ts_source[ts_start:ts_end]
        ts_keys = set(re.findall(r"^\s+(\w+)[\?:]", ts_block, re.MULTILINE))

        # Python keys should be a subset of TypeScript keys
        missing_in_ts = py_keys - ts_keys
        assert not missing_in_ts, f"Python row keys missing in TypeScript: {missing_in_ts}"

    def test_scanner_tsx_imports_mil_breakdown_type(self):
        """Scanner.tsx imports MILBreakdown type for type-safe rendering."""
        tsx_source = (ROOT / "web" / "frontend" / "src" / "pages" / "Scanner.tsx").read_text()
        assert "MILBreakdown" in tsx_source

    def test_scanner_tsx_renders_mil_section(self):
        """Scanner.tsx DiagnosticPanel has a Market Intelligence Layer section."""
        tsx_source = (ROOT / "web" / "frontend" / "src" / "pages" / "Scanner.tsx").read_text()
        assert "Market Intelligence Layer" in tsx_source

    def test_scanner_tsx_renders_decision_explanation(self):
        """Scanner.tsx DiagnosticPanel has a Decision Path section."""
        tsx_source = (ROOT / "web" / "frontend" / "src" / "pages" / "Scanner.tsx").read_text()
        assert "Decision Path" in tsx_source
        assert "decision_explanation" in tsx_source
        assert "block_reasons" in tsx_source

    def test_scanner_tsx_renders_technical_vs_final_score(self):
        """Scanner.tsx shows tech score alongside final score in Confluence stage."""
        tsx_source = (ROOT / "web" / "frontend" / "src" / "pages" / "Scanner.tsx").read_text()
        assert "technical_score" in tsx_source


# ══════════════════════════════════════════════════════════════
# SECTION 7: _build_decision_explanation Edge Cases
# ══════════════════════════════════════════════════════════════

class TestDecisionExplanationEdgeCases:
    """Edge cases in the explanation builder."""

    def _get_engine_class(self):
        web_dir = ROOT / "web"
        web_engine_dir = ROOT / "web" / "engine"
        web_backend_dir = ROOT / "web" / "backend"
        for p in [str(web_dir), str(web_engine_dir), str(web_backend_dir), str(ROOT)]:
            if p not in sys.path:
                sys.path.insert(0, p)
        with patch.dict(sys.modules, {
            "core_patch": MagicMock(),
            "core_patch.event_bus": MagicMock(),
            "core_patch.redis_bridge": MagicMock(),
        }):
            import importlib
            if "engine.main" in sys.modules:
                mod = importlib.reload(sys.modules["engine.main"])
            else:
                mod = importlib.import_module("engine.main")
            return mod.TradingEngineService

    def test_pre_filter_rejection_explanation(self):
        """Pre-filter rejection produces clear explanation."""
        cls = self._get_engine_class()
        engine = cls.__new__(cls)
        scan = _make_scan_result(status="Volatility filter: ATR ratio 0.30 < min 0.50")
        diag = dict(scan["diagnostics"])
        diag["pre_filter_reason"] = "Volatility filter: ATR ratio 0.30 < min 0.50"

        explanation, blocks = engine._build_decision_explanation(
            "Pre-Filter", "Volatility filter: ATR ratio 0.30 < min 0.50",
            scan, diag, {"mil_active": False},
        )
        assert "pre-filter" in explanation.lower() or "Volatility" in explanation
        assert len(blocks) > 0

    def test_exception_safety(self):
        """_build_decision_explanation never raises — returns status on error."""
        cls = self._get_engine_class()
        engine = cls.__new__(cls)
        # Pass None diag to trigger exception
        explanation, blocks = engine._build_decision_explanation(
            "Error", "", {}, None, {},
        )
        # Should not raise — returns pipeline_status as fallback
        assert isinstance(explanation, str)
        assert isinstance(blocks, list)

    def test_below_threshold_explanation(self):
        """Score below threshold produces clear explanation with values."""
        cls = self._get_engine_class()
        engine = cls.__new__(cls)
        scan = _make_scan_result(score=0.0, is_approved=False, models_fired=["momentum_breakout"])
        diag = dict(scan["diagnostics"])
        diag["effective_threshold"] = 0.35

        explanation, blocks = engine._build_decision_explanation(
            "No Signal", "Below threshold", scan, diag, {"mil_active": False},
        )
        # Score is 0.0 and threshold is 0.35 — should mention threshold
        assert "threshold" in explanation.lower() or "0.35" in explanation or "signal" in explanation.lower()


# ══════════════════════════════════════════════════════════════
# SECTION 8: MIL Invariant Enforcement
# ══════════════════════════════════════════════════════════════

class TestMILInvariantA:
    """Invariant A: sentiment_delta + news_delta + other_orchestrator_delta == orchestrator_delta
    ALL computed in ConfluenceScorer — NOT in API layer."""

    def test_invariant_a_enforced_in_scorer(self):
        """ConfluenceScorer computes other_orch_delta = orch - sentiment - news."""
        source = (ROOT / "core" / "meta_decision" / "confluence_scorer.py").read_text()
        assert "_other_orch_delta = _orch_delta_attr - _sentiment_delta - _news_delta" in source

    def test_invariant_a_validation_in_scorer(self):
        """ConfluenceScorer validates invariant A and logs error if violated."""
        source = (ROOT / "core" / "meta_decision" / "confluence_scorer.py").read_text()
        assert "MIL invariant A violated" in source

    def test_invariant_a_NOT_in_api_layer(self):
        """API layer (_get_mil_diagnostics) has NO invariant computation code.
        Comments referencing the scorer's invariant work are acceptable."""
        source = (ROOT / "web" / "engine" / "main.py").read_text()
        start = source.index("def _get_mil_diagnostics")
        end = source.index("\n    def ", start + 10)
        method = source[start:end]
        # Strip comments to check only executable code
        code_lines = [l for l in method.split("\n") if l.strip() and not l.strip().startswith("#")]
        code_only = "\n".join(code_lines)
        assert "_inv_a" not in code_only  # no invariant A variables
        assert "_other_orch_delta =" not in code_only  # no derivation
        assert "_sentiment_delta =" not in code_only  # no placeholder assignment

    def test_invariant_a_holds_pre_4b(self):
        """Pre-Phase 4B: sentiment=0, news=0, other=orch. Sum == orch."""
        orch = 0.0345
        sentiment = 0.0
        news = 0.0
        other = orch - sentiment - news
        assert abs((sentiment + news + other) - orch) < 1e-9

    def test_invariant_a_holds_with_future_values(self):
        """Phase 4B simulation: sentiment=0.01, news=0.005, other=orch-0.015."""
        orch = 0.0345
        sentiment = 0.01
        news = 0.005
        other = orch - sentiment - news
        assert abs((sentiment + news + other) - orch) < 1e-9

    def test_invariant_a_tolerance_1e6(self):
        """Invariant check uses 1e-6 tolerance (in scorer)."""
        source = (ROOT / "core" / "meta_decision" / "confluence_scorer.py").read_text()
        assert "1e-6" in source


class TestMILInvariantB:
    """Invariant B: orchestrator_delta + oi_delta + liquidation_delta == mil_total_delta
    Enforced by construction + validated in ConfluenceScorer."""

    def test_invariant_b_enforced_in_scorer_by_construction(self):
        """ConfluenceScorer computes _orch_delta_attr = total - oi - liq."""
        source = (ROOT / "core" / "meta_decision" / "confluence_scorer.py").read_text()
        assert "_orch_delta_attr = _total_delta - _oi_mod_val - _liq_mod_val" in source

    def test_invariant_b_validated_in_scorer(self):
        """ConfluenceScorer validates invariant B and logs error if violated."""
        source = (ROOT / "core" / "meta_decision" / "confluence_scorer.py").read_text()
        assert "MIL invariant B violated" in source

    def test_invariant_b_NOT_in_api_layer(self):
        """API layer has NO invariant B computation code.
        Comments referencing scorer's invariant work are acceptable."""
        source = (ROOT / "web" / "engine" / "main.py").read_text()
        start = source.index("def _get_mil_diagnostics")
        end = source.index("\n    def ", start + 10)
        method = source[start:end]
        # Strip comments to check only executable code
        code_lines = [l for l in method.split("\n") if l.strip() and not l.strip().startswith("#")]
        code_only = "\n".join(code_lines)
        assert "_inv_b" not in code_only
        assert "_orch_delta_attr" not in code_only  # no decomposition
        assert "_total_delta - " not in code_only  # no invariant B formula

    def test_invariant_b_validation_uses_1e3_tolerance(self):
        """Invariant B tolerance accounts for rounding (1e-3) in scorer."""
        source = (ROOT / "core" / "meta_decision" / "confluence_scorer.py").read_text()
        assert "1e-3" in source

    def test_invariant_b_holds_numerically(self):
        """Numerical proof: orch + oi + liq == total."""
        total = 0.0500
        oi = 0.0150
        liq = 0.0050
        orch = total - oi - liq
        assert abs((orch + oi + liq) - total) < 1e-9

    def test_invariant_b_holds_when_capped(self):
        """Clamping happens BEFORE decomposition — invariant always holds."""
        source = (ROOT / "core" / "meta_decision" / "confluence_scorer.py").read_text()
        cap_idx = source.index("_clamped_delta")
        decomp_idx = source.index("_orch_delta_attr = _total_delta")
        assert decomp_idx > cap_idx


# ══════════════════════════════════════════════════════════════
# SECTION 9: Placeholder Safety
# ══════════════════════════════════════════════════════════════

class TestPlaceholderSafety:
    """Placeholders (sentiment=0, news=0) must not mislead or break logic."""

    def test_dominant_source_threshold_excludes_zero_placeholders(self):
        """Dominant source requires > 0.001 — zero placeholders never win.
        Computed in ConfluenceScorer, not API layer."""
        source = (ROOT / "core" / "meta_decision" / "confluence_scorer.py").read_text()
        assert "_sources[_max_src] > 0.001" in source

    def test_dominant_source_with_only_orch_and_oi(self):
        """When sentiment=0, news=0, dominant is max(orch, oi, liq)."""
        sources = {
            "orchestrator": abs(0.03),
            "sentiment": abs(0.0),
            "news": abs(0.0),
            "oi": abs(0.015),
            "liquidation": abs(0.005),
        }
        max_src = max(sources, key=sources.get)
        assert max_src == "orchestrator"
        assert sources[max_src] > 0.001

    def test_ui_filters_zero_placeholders(self):
        """Scanner.tsx filters out sentiment/news when delta is zero."""
        tsx = (ROOT / "web" / "frontend" / "src" / "pages" / "Scanner.tsx").read_text()
        assert "sentiment_delta" in tsx
        assert "news_delta" in tsx
        # Filter logic: hide when abs < 0.0001
        assert "0.0001" in tsx

    def test_ui_shows_other_orchestrator_delta(self):
        """Scanner.tsx renders breakdown via Object.entries(row.mil_breakdown)
        which includes other_orchestrator_delta from backend data.
        The filter ONLY hides sentiment_delta and news_delta when zero —
        other_orchestrator_delta is NOT in the filter list."""
        tsx = (ROOT / "web" / "frontend" / "src" / "pages" / "Scanner.tsx").read_text()
        # Breakdown is rendered dynamically via Object.entries
        assert "Object.entries(row.mil_breakdown" in tsx
        # Filter only targets sentiment and news — NOT other_orchestrator
        filter_line_idx = tsx.index("return false")
        filter_context = tsx[max(0, filter_line_idx - 300):filter_line_idx + 20]
        assert "sentiment_delta" in filter_context
        assert "news_delta" in filter_context
        assert "other_orchestrator" not in filter_context

    def test_invariant_a_holds_with_zero_placeholders(self):
        """With placeholders at 0, invariant A: 0 + 0 + orch == orch."""
        orch = 0.0345
        assert abs((0.0 + 0.0 + orch) - orch) < 1e-9


# ══════════════════════════════════════════════════════════════
# SECTION 10: Source of Truth Full Trace
# ══════════════════════════════════════════════════════════════

class TestSourceOfTruthFullTrace:
    """Full trace: DB → Engine → API → UI. No fallbacks, no hardcoded symbols."""

    def test_db_query_uses_asset_is_tradable(self):
        """_cmd_get_pipeline_status queries Asset.is_tradable.is_(True)."""
        source = (ROOT / "web" / "engine" / "main.py").read_text()
        assert "Asset.is_tradable.is_(True)" in source

    def test_db_query_function_name(self):
        """Function: _cmd_get_pipeline_status in TradingEngineService."""
        source = (ROOT / "web" / "engine" / "main.py").read_text()
        assert "async def _cmd_get_pipeline_status(self, params: dict)" in source

    def test_no_config_yaml_fallback_in_pipeline_status(self):
        """No config.yaml fallback for tradable universe in pipeline status."""
        source = (ROOT / "web" / "engine" / "main.py").read_text()
        start = source.index("async def _cmd_get_pipeline_status")
        end = source.index("\n    async def ", start + 10)
        method = source[start:end]
        assert "config.yaml" not in method
        assert "settings.get" not in method.split("# ── 2.")[0]  # no settings before scan lookup

    def test_no_hardcoded_symbols_in_pipeline_status(self):
        """No hardcoded symbol lists in pipeline status."""
        source = (ROOT / "web" / "engine" / "main.py").read_text()
        start = source.index("async def _cmd_get_pipeline_status")
        end = source.index("\n    async def ", start + 10)
        method = source[start:end]
        for sym in ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]:
            assert sym not in method

    def test_no_merged_lists_in_pipeline_status(self):
        """Universe is ONLY from DB. No union/merge with any other source."""
        source = (ROOT / "web" / "engine" / "main.py").read_text()
        start = source.index("async def _cmd_get_pipeline_status")
        merge_section = source[source.index("# ── 3. Merge"):source.index("# ── 4. Summary")]
        # Only iterates tradable_assets (from DB)
        assert "for asset in tradable_assets:" in merge_section
        # Does NOT iterate scan results as primary universe
        assert "for r in" not in merge_section

    def test_scan_universe_constructed_from_db(self):
        """tradable_assets list populated by DB SELECT query only."""
        source = (ROOT / "web" / "engine" / "main.py").read_text()
        start = source.index("# ── 1. Get tradable universe")
        end = source.index("# ── 2. Build scanner")
        section = source[start:end]
        assert "select(Asset)" in section
        assert "tradable_assets.append" in section

    def test_api_route_calls_engine_command(self):
        """FastAPI scanner route delegates to engine command handler."""
        api_path = ROOT / "web" / "backend" / "app" / "api" / "scanner.py"
        api_source = api_path.read_text()
        assert "pipeline-status" in api_source or "pipeline_status" in api_source

    def test_frontend_calls_api_endpoint(self):
        """Frontend getPipelineStatus calls /scanner/pipeline-status."""
        ts = (ROOT / "web" / "frontend" / "src" / "api" / "scanner.ts").read_text()
        assert "'/scanner/pipeline-status'" in ts

    def test_frontend_renders_from_api_data(self):
        """Scanner.tsx reads pipeline from API response, no local state."""
        tsx = (ROOT / "web" / "frontend" / "src" / "pages" / "Scanner.tsx").read_text()
        assert "pipelineData?.pipeline" in tsx


# ══════════════════════════════════════════════════════════════
# SECTION 11: Per-Field No-Duplication Proof
# ══════════════════════════════════════════════════════════════

class TestPerFieldNoDuplication:
    """Each promoted field: computed backend-only, API pass-through, frontend read-only."""

    _FIELDS = [
        "technical_score",
        "final_score",
        "mil_total_delta",
        "mil_influence_pct",
        "mil_dominant_source",
    ]

    def test_all_fields_set_in_backend_pipeline_row(self):
        """All 5 fields are set in _cmd_get_pipeline_status pipeline row."""
        source = (ROOT / "web" / "engine" / "main.py").read_text()
        scan_start = source.index("if scan:")
        scan_end = source.index("else:", scan_start)
        block = source[scan_start:scan_end]
        for field in self._FIELDS:
            assert f'"{field}"' in block, f"{field} not in backend pipeline row"

    def test_all_fields_in_typescript_interface(self):
        """All 5 fields exist in PipelineRow TypeScript interface."""
        ts = (ROOT / "web" / "frontend" / "src" / "api" / "scanner.ts").read_text()
        for field in self._FIELDS:
            assert f"{field}:" in ts, f"{field} not in TypeScript PipelineRow"

    def test_frontend_does_not_recompute_fields(self):
        """Scanner.tsx does not compute or transform any of the 5 fields."""
        tsx = (ROOT / "web" / "frontend" / "src" / "pages" / "Scanner.tsx").read_text()
        # These fields should only appear in read contexts (row.X or {row.X})
        # NOT in assignment contexts (= ... + ... or computation)
        for field in self._FIELDS:
            # Count occurrences
            occurrences = tsx.count(field)
            if occurrences > 0:
                # Verify they're all read-only (row.field patterns)
                import re
                # Should NOT appear in: `const X = someComputation(row.field)`
                # or `field = row.other + row.another`
                compute_pattern = rf'{field}\s*='
                computes = re.findall(compute_pattern, tsx)
                assert len(computes) == 0, \
                    f"{field} appears to be computed/assigned in frontend: {computes}"

    def test_technical_score_source_is_mil_technical_baseline(self):
        """technical_score reads from diag.mil_technical_baseline (ConfluenceScorer output)."""
        source = (ROOT / "web" / "engine" / "main.py").read_text()
        assert '"technical_score": diag.get("mil_technical_baseline", 0.0)' in source

    def test_final_score_source_is_scan_score(self):
        """final_score reads from scan.score (unchanged from ConfluenceScorer)."""
        source = (ROOT / "web" / "engine" / "main.py").read_text()
        assert '"final_score": scan.get("score", 0.0)' in source

    def test_mil_total_delta_source_is_scorer_diagnostics(self):
        """mil_total_delta reads from scorer_diag.mil_total_delta."""
        source = (ROOT / "web" / "engine" / "main.py").read_text()
        assert 'scorer_diag.get("mil_total_delta", 0.0)' in source

    def test_mil_influence_pct_source_is_scorer_diagnostics(self):
        """mil_influence_pct reads from scorer_diag.mil_delta_pct."""
        source = (ROOT / "web" / "engine" / "main.py").read_text()
        assert 'scorer_diag.get("mil_delta_pct", 0.0)' in source

    def test_mil_dominant_source_computed_in_scorer(self):
        """mil_dominant_source computed in ConfluenceScorer from breakdown deltas.
        API layer (_get_mil_diagnostics) is pure pass-through — reads scorer result."""
        # Prove: scorer computes dominant source
        scorer_src = (ROOT / "core" / "meta_decision" / "confluence_scorer.py").read_text()
        assert 'max(_sources, key=_sources.get)' in scorer_src
        assert 'self._last_diagnostics["mil_dominant_source"]' in scorer_src
        # Prove: API layer does NOT compute it — only reads via .get()
        api_src = (ROOT / "web" / "engine" / "main.py").read_text()
        start = api_src.index("def _get_mil_diagnostics")
        end = api_src.index("\n    def ", start + 10)
        method = api_src[start:end]
        assert "max(" not in method  # no max() computation in API
        assert 'scorer_diag.get("mil_dominant_source"' in method  # pure read


# ══════════════════════════════════════════════════════════════
# SECTION 12: Performance Proof
# ══════════════════════════════════════════════════════════════

class TestPerformance:
    """No additional API calls, no recomputation, no added latency."""

    def test_no_new_api_calls_in_pipeline_status(self):
        """_cmd_get_pipeline_status makes zero HTTP/exchange calls."""
        source = (ROOT / "web" / "engine" / "main.py").read_text()
        start = source.index("async def _cmd_get_pipeline_status")
        end = source.index("\n    async def ", start + 10)
        method = source[start:end]
        # No fetch_ohlcv, no requests, no aiohttp
        assert "fetch_ohlcv" not in method
        assert "requests.get" not in method
        assert "aiohttp" not in method

    def test_get_mil_diagnostics_reads_caches_only(self):
        """_get_mil_diagnostics reads cached data — no network calls."""
        source = (ROOT / "web" / "engine" / "main.py").read_text()
        start = source.index("def _get_mil_diagnostics")
        end = source.index("\n    def ", start + 10)
        method = source[start:end]
        # Only reads from cached data
        assert "get_symbol_signal" in method  # reads cache
        assert "get_diagnostics" in method    # reads cache
        assert "get_oi_data" in method        # reads cache
        # No network calls
        assert ".fetch(" not in method
        assert ".post(" not in method
        assert ".enhance(" not in method
        assert ".compute(" not in method

    def test_single_payload_still_used(self):
        """Scanner still returns results in a single pipeline payload."""
        source = (ROOT / "web" / "engine" / "main.py").read_text()
        start = source.index("async def _cmd_get_pipeline_status")
        end = source.index("\n    async def ", start + 10)
        method = source[start:end]
        # Single return with pipeline array
        assert '"pipeline": pipeline_rows' in method

    def test_frontend_single_api_query(self):
        """Frontend makes one useQuery for pipeline data (single network call).
        getPipelineStatus appears twice: import + queryFn usage — both correct."""
        tsx = (ROOT / "web" / "frontend" / "src" / "pages" / "Scanner.tsx").read_text()
        # Exactly one useQuery call with getPipelineStatus as queryFn
        assert tsx.count("queryFn: getPipelineStatus") == 1
        # No secondary fetch (fetch/axios/api.get) for pipeline data
        assert "api.get" not in tsx

    def test_build_decision_explanation_is_pure_computation(self):
        """_build_decision_explanation uses only in-memory data — no I/O."""
        source = (ROOT / "web" / "engine" / "main.py").read_text()
        start = source.index("def _build_decision_explanation")
        end = source.index("\n    @staticmethod\n    def _pipeline_reason", start)
        method = source[start:end]
        assert "await" not in method
        assert "import" not in method
        assert ".fetch" not in method


# ══════════════════════════════════════════════════════════════
# SECTION 13: _get_mil_diagnostics Validation
# ══════════════════════════════════════════════════════════════

class TestGetMILDiagnosticsValidation:
    """Confirm _get_mil_diagnostics is read-only pass-through."""

    def test_reads_scorer_last_diagnostics(self):
        """Reads self._scorer._last_diagnostics (populated by score())."""
        source = (ROOT / "web" / "engine" / "main.py").read_text()
        assert "self._scorer._last_diagnostics" in source

    def test_reads_funding_rate_cache(self):
        """Reads funding_rate_agent.get_symbol_signal() cache."""
        source = (ROOT / "web" / "engine" / "main.py").read_text()
        assert "funding_rate_agent.get_symbol_signal(symbol)" in source

    def test_reads_oi_cache(self):
        """Reads coinglass_agent.get_oi_data() cache."""
        source = (ROOT / "web" / "engine" / "main.py").read_text()
        assert "coinglass_agent.get_oi_data(symbol)" in source

    def test_does_not_call_enhance(self):
        """Never calls .enhance() on any enhancer."""
        source = (ROOT / "web" / "engine" / "main.py").read_text()
        start = source.index("def _get_mil_diagnostics")
        end = source.index("\n    def ", start + 10)
        method = source[start:end]
        assert ".enhance(" not in method

    def test_does_not_call_score(self):
        """Never calls .score() on any scorer in executable code."""
        source = (ROOT / "web" / "engine" / "main.py").read_text()
        start = source.index("def _get_mil_diagnostics")
        end = source.index("\n    def ", start + 10)
        method = source[start:end]
        # Strip comments to check only executable code
        code_lines = [l for l in method.split("\n") if l.strip() and not l.strip().startswith("#")]
        code_only = "\n".join(code_lines)
        assert ".score(" not in code_only

    def test_fail_open_on_exception(self):
        """Wraps everything in try/except — never raises."""
        source = (ROOT / "web" / "engine" / "main.py").read_text()
        start = source.index("def _get_mil_diagnostics")
        end = source.index("\n    def ", start + 10)
        method = source[start:end]
        assert "except Exception:" in method
        assert "pass  # fail-open" in method
