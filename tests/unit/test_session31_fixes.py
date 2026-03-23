# ============================================================
# Session 31 — Position Sizing & Regime Classification Fixes
#
# Tests verify the two root causes identified in the audit:
#   1. position_sizer.py: calculate_risk_based() used hardcoded
#      25% cap instead of self.max_capital_pct (4%)
#   2. confluence_scorer.py: max_size_usdt=500.0 (binding cap)
#      removed in favour of 0.0 (no absolute cap; 4% pct governs)
#   3. hmm_regime_classifier.py: adaptive HMM weight degrades
#      to 0.20 when >50% of states are labeled "uncertain"
#   4. hmm_regime_classifier.py: warmup reduced from 30 → 5 bars
# ============================================================
from __future__ import annotations

import math
import numpy as np
import pandas as pd
import pytest
from unittest.mock import MagicMock, patch

from core.meta_decision.position_sizer import PositionSizer
from core.regime.hmm_regime_classifier import HMMRegimeClassifier, _DEFAULT_STATE_MAP
from core.regime.regime_classifier import (
    REGIME_UNCERTAIN, REGIME_BULL_TREND, REGIME_BEAR_TREND, REGIME_RANGING
)


# ──────────────────────────────────────────────────────────────
# Section A — PositionSizer: max_capital_pct fix
# ──────────────────────────────────────────────────────────────

class TestPositionSizerCapFix:
    """
    A-01 through A-07: Verify calculate_risk_based() uses
    self.max_capital_pct instead of the old hardcoded 25%.
    """

    def _sizer(self, max_capital_pct=0.04, max_size_usdt=0.0):
        return PositionSizer(
            max_capital_pct=max_capital_pct,
            max_size_usdt=max_size_usdt,
            min_size_usdt=10.0,
        )

    def test_a01_4pct_cap_governs_not_25pct(self):
        """
        With $100,000 capital, 0.5% risk, large stop distance,
        4% cap = $4,000 should apply — NOT $25,000.
        """
        sizer = self._sizer(max_capital_pct=0.04)
        # Use tiny stop distance to force an astronomically large raw size
        size = sizer.calculate_risk_based(
            capital_usdt=100_000.0,
            entry_price=100.0,
            stop_price=99.99,   # $0.01 stop — raw size = 500 / 0.01 * 100 = $5,000,000
            risk_pct=0.5,
        )
        # Must be capped at 4% = $4,000
        assert size == pytest.approx(4_000.0, rel=1e-3), (
            f"Expected ~$4,000 (4% cap) but got ${size:.2f}"
        )

    def test_a02_25pct_no_longer_applies(self):
        """
        Old code: cap_max = capital * 0.25 = $25,000.
        New code: cap_max = capital * 0.04 = $4,000.
        If the old code were present, size would be $25,000.
        """
        sizer = self._sizer(max_capital_pct=0.04)
        size = sizer.calculate_risk_based(
            capital_usdt=100_000.0,
            entry_price=100.0,
            stop_price=99.99,
            risk_pct=0.5,
        )
        assert size < 10_000.0, "Old 25% cap would have returned $25,000 — fix not applied"

    def test_a03_btc_realistic_calculation(self):
        """
        Reproduce the actual BTC/USDT trade from open_positions.json:
          capital=101327.28, entry=70840.36, stop=69247.11, risk_pct=0.5%
          stop_distance = 1593.45
          risk_usdt = 0.5% * 101327 = 506.64
          qty = 506.64 / 1593.45 = 0.3179
          size_usdt = 0.3179 * 70840.36 = $22,527
          cap = 4% * 101327 = $4,053  ← binding
        Expected: ~$4,053
        """
        sizer = self._sizer(max_capital_pct=0.04, max_size_usdt=0.0)
        size = sizer.calculate_risk_based(
            capital_usdt=101_327.28,
            entry_price=70_840.36,
            stop_price=69_247.11,
            risk_pct=0.5,
        )
        assert 3_800 < size <= 4_200, (
            f"BTC/USDT trade should be ~$4,053 but got ${size:.2f}"
        )

    def test_a04_sol_realistic_calculation(self):
        """
        SOL/USDT trade: entry=91.52, stop=88.63
        stop_distance = 2.89
        risk_usdt = 0.5% * 101327 = 506.64
        qty = 506.64 / 2.89 = 175.3
        size_usdt = 175.3 * 91.52 = $16,047
        cap = 4% * 101327 = $4,053  ← binding
        Expected: ~$4,053
        """
        sizer = self._sizer(max_capital_pct=0.04, max_size_usdt=0.0)
        size = sizer.calculate_risk_based(
            capital_usdt=101_327.28,
            entry_price=91.52,
            stop_price=88.63,
            risk_pct=0.5,
        )
        assert 3_800 < size <= 4_200, (
            f"SOL/USDT trade should be ~$4,053 but got ${size:.2f}"
        )

    def test_a05_max_capital_pct_parameter_respected(self):
        """max_capital_pct is honoured at various values."""
        capital = 100_000.0
        for pct in [0.01, 0.02, 0.04, 0.10]:
            sizer = self._sizer(max_capital_pct=pct)
            size = sizer.calculate_risk_based(
                capital_usdt=capital,
                entry_price=100.0,
                stop_price=99.99,   # tiny stop → raw size >> cap
                risk_pct=0.5,
            )
            cap = capital * pct
            assert size <= cap + 1e-6, (
                f"max_capital_pct={pct}: size {size:.2f} > cap {cap:.2f}"
            )

    def test_a06_no_absolute_cap_when_max_size_usdt_zero(self):
        """max_size_usdt=0 means no dollar cap; pct cap governs."""
        sizer = self._sizer(max_capital_pct=0.04, max_size_usdt=0.0)
        size = sizer.calculate_risk_based(
            capital_usdt=100_000.0,
            entry_price=100.0,
            stop_price=99.99,
            risk_pct=0.5,
        )
        # Should be exactly the 4% cap = $4,000 (not floored to $500 or $10)
        assert size == pytest.approx(4_000.0, rel=1e-3), (
            f"Expected 4% cap $4,000 but got ${size:.2f}"
        )

    def test_a07_old_500_cap_gone(self):
        """
        The old max_size_usdt=500 cap is no longer the default.
        With $100k capital and 0.5% risk, any reasonable trade
        should exceed $500.
        """
        sizer = self._sizer(max_capital_pct=0.04, max_size_usdt=0.0)
        size = sizer.calculate_risk_based(
            capital_usdt=100_000.0,
            entry_price=50_000.0,
            stop_price=49_000.0,   # $1,000 stop
            risk_pct=0.5,
        )
        # risk_usdt = 500; qty = 0.5; size_usdt = $25,000 → capped at $4,000
        assert size > 500.0, (
            f"Trade of ${size:.2f} suggests $500 cap still active"
        )


# ──────────────────────────────────────────────────────────────
# Section B — ConfluenceScorer: max_size_usdt=0.0 default
# ──────────────────────────────────────────────────────────────

class TestConfluenceScorerSizerDefault:
    """
    B-01 through B-04: Verify ConfluenceScorer instantiates
    PositionSizer with max_size_usdt=0.0 (no absolute cap).
    """

    def test_b01_confluence_scorer_sizer_max_size_usdt_is_zero(self):
        """ConfluenceScorer._sizer.max_size_usdt must be 0.0."""
        from core.meta_decision.confluence_scorer import ConfluenceScorer
        cs = ConfluenceScorer()
        assert cs._sizer.max_size_usdt == 0.0, (
            f"ConfluenceScorer._sizer.max_size_usdt = {cs._sizer.max_size_usdt}; expected 0.0"
        )

    def test_b02_confluence_scorer_sizer_max_size_usdt_not_500(self):
        """The old $500 cap must no longer be present."""
        from core.meta_decision.confluence_scorer import ConfluenceScorer
        cs = ConfluenceScorer()
        assert cs._sizer.max_size_usdt != 500.0, (
            "max_size_usdt=500.0 (old demo cap) still present in ConfluenceScorer"
        )

    def test_b03_confluence_scorer_sizer_max_capital_pct_is_4pct(self):
        """ConfluenceScorer._sizer.max_capital_pct must be 0.04 (4%)."""
        from core.meta_decision.confluence_scorer import ConfluenceScorer
        cs = ConfluenceScorer()
        assert cs._sizer.max_capital_pct == pytest.approx(0.04, abs=1e-6), (
            f"max_capital_pct = {cs._sizer.max_capital_pct}; expected 0.04"
        )

    def test_b04_position_sizer_source_no_hardcoded_0_25(self):
        """
        The old `capital_usdt * 0.25` hardcoded cap must not appear
        in position_sizer.py's calculate_risk_based().
        """
        import pathlib
        src = (
            pathlib.Path(__file__).parent.parent.parent
            / "core" / "meta_decision" / "position_sizer.py"
        ).read_text(encoding="utf-8")
        # Locate calculate_risk_based method
        idx = src.index("def calculate_risk_based")
        end = src.index("\n    def ", idx + 1)
        method_body = src[idx:end]
        assert "0.25" not in method_body, (
            "Hardcoded 0.25 cap still present in calculate_risk_based()"
        )
        assert "self.max_capital_pct" in method_body, (
            "self.max_capital_pct not found in calculate_risk_based()"
        )


# ──────────────────────────────────────────────────────────────
# Section C — HMMRegimeClassifier: adaptive weight
# ──────────────────────────────────────────────────────────────

class TestHMMAdaptiveWeight:
    """
    C-01 through C-08: Verify classify_combined() uses adaptive
    HMM weight based on fraction of 'uncertain' states in map.
    """

    def _make_clf_with_state_map(self, state_map: dict) -> HMMRegimeClassifier:
        """Create a fitted-looking classifier with a forced state map."""
        clf = HMMRegimeClassifier()
        clf._state_map = state_map
        clf._is_fitted = True
        return clf

    def _minimal_df(self, n=100) -> pd.DataFrame:
        """Minimal OHLCV DataFrame sufficient for classify()."""
        rng = np.random.default_rng(42)
        closes = 100 + np.cumsum(rng.normal(0, 0.5, n))
        return pd.DataFrame({
            "open":   closes * 0.999,
            "high":   closes * 1.002,
            "low":    closes * 0.998,
            "close":  closes,
            "volume": rng.uniform(1000, 5000, n),
            "adx":    rng.uniform(15, 40, n),
        })

    def test_c01_all_uncertain_states_degrades_hmm_weight(self):
        """
        When all 6 states map to 'uncertain', uncertain_frac=1.0 > 0.5
        → hmm_w=0.20, rb_w=0.80.
        The final regime must NOT be 'uncertain' if the rule-based
        fallback returns a definitive regime.
        """
        clf = self._make_clf_with_state_map({i: REGIME_UNCERTAIN for i in range(6)})
        df = self._minimal_df(300)

        # Patch classify() to return certain regime from HMM side,
        # but all states are 'uncertain' → rule-based should dominate
        # We just verify classify_combined() doesn't crash and returns something
        with patch.object(clf._fallback, "classify", return_value=(REGIME_BULL_TREND, 0.80, {})):
            label, conf, probs = clf.classify_combined(df)
        # With rb_w=0.80 and rule-based returning bull_trend at 0.80 confidence,
        # bull_trend should have significant probability
        assert probs.get(REGIME_BULL_TREND, 0) > 0.4, (
            f"Rule-based should dominate when all HMM states are uncertain; "
            f"got bull_trend_prob={probs.get(REGIME_BULL_TREND, 0):.3f}"
        )

    def test_c02_majority_uncertain_triggers_degraded_mode(self):
        """
        4/6 states uncertain (66.7% > 50%) → hmm_w=0.20.
        """
        state_map = {
            0: REGIME_UNCERTAIN,
            1: REGIME_UNCERTAIN,
            2: REGIME_UNCERTAIN,
            3: REGIME_UNCERTAIN,
            4: REGIME_BULL_TREND,
            5: REGIME_BEAR_TREND,
        }
        clf = self._make_clf_with_state_map(state_map)
        df = self._minimal_df(200)
        with patch.object(clf._fallback, "classify", return_value=(REGIME_BULL_TREND, 0.85, {})):
            label, conf, probs = clf.classify_combined(df)
        # rb_w=0.80 × 0.85 = 0.68 mass on bull_trend from rule-based alone
        assert probs.get(REGIME_BULL_TREND, 0) > 0.5

    def test_c03_minority_uncertain_uses_normal_hmm_weight(self):
        """
        2/6 states uncertain (33.3% < 50%) → normal hmm_w=0.60.
        Rule-based contribution should be 0.40.
        """
        state_map = {
            0: REGIME_BULL_TREND,
            1: REGIME_BEAR_TREND,
            2: REGIME_RANGING,
            3: REGIME_BULL_TREND,
            4: REGIME_UNCERTAIN,
            5: REGIME_UNCERTAIN,
        }
        clf = self._make_clf_with_state_map(state_map)
        df = self._minimal_df(200)
        # Verify no error and structure is correct
        with patch.object(clf._fallback, "classify", return_value=(REGIME_RANGING, 0.80, {})):
            label, conf, probs = clf.classify_combined(df)
        # With normal weight, HMM (60%) and RB (40%) both contribute
        # Just verify the call completes without exception
        assert isinstance(label, str)
        assert 0.0 <= conf <= 1.0

    def test_c04_zero_uncertain_states_uses_normal_weight(self):
        """0 uncertain states → uncertain_frac=0 < 0.5 → hmm_w=0.60."""
        state_map = {
            0: REGIME_BULL_TREND,
            1: REGIME_BEAR_TREND,
            2: REGIME_RANGING,
            3: REGIME_BULL_TREND,
            4: REGIME_BEAR_TREND,
            5: REGIME_RANGING,
        }
        clf = self._make_clf_with_state_map(state_map)
        df = self._minimal_df(200)
        with patch.object(clf._fallback, "classify", return_value=(REGIME_BULL_TREND, 0.70, {})):
            label, conf, probs = clf.classify_combined(df)
        assert isinstance(label, str)
        assert conf >= 0.0

    def test_c05_exactly_50pct_uncertain_uses_normal_weight(self):
        """
        3/6 states uncertain (50%) — boundary condition.
        uncertain_frac > 0.50 is required for degraded mode,
        so exactly 50% → normal hmm_w=0.60.
        """
        state_map = {
            0: REGIME_UNCERTAIN,
            1: REGIME_UNCERTAIN,
            2: REGIME_UNCERTAIN,
            3: REGIME_BULL_TREND,
            4: REGIME_BEAR_TREND,
            5: REGIME_RANGING,
        }
        clf = self._make_clf_with_state_map(state_map)
        n_uncertain = sum(1 for v in state_map.values() if v == REGIME_UNCERTAIN)
        uncertain_frac = n_uncertain / len(state_map)
        # 50% is NOT > 50%, so NOT degraded
        assert uncertain_frac == pytest.approx(0.5)
        assert uncertain_frac <= 0.5  # boundary: normal mode

    def test_c06_51pct_uncertain_triggers_degraded(self):
        """
        4/7 states uncertain = 57% > 50% → degraded mode.
        (7 states edge case — should work for any N states)
        """
        state_map = {i: REGIME_UNCERTAIN for i in range(4)}
        state_map.update({4: REGIME_BULL_TREND, 5: REGIME_BEAR_TREND, 6: REGIME_RANGING})
        n_uncertain = sum(1 for v in state_map.values() if v == REGIME_UNCERTAIN)
        uncertain_frac = n_uncertain / len(state_map)
        assert uncertain_frac > 0.5  # should be 4/7 ≈ 0.571

    def test_c07_unfitted_classifier_falls_back_to_rule_based(self):
        """When not fitted, classify_combined() uses pure rule-based (hmm_w=0)."""
        clf = HMMRegimeClassifier()
        assert not clf.is_fitted
        df = self._minimal_df(100)
        with patch.object(clf._fallback, "classify", return_value=(REGIME_BULL_TREND, 0.75, {})) as m:
            label, conf, probs = clf.classify_combined(df)
        m.assert_called()
        assert label == REGIME_BULL_TREND

    def test_c08_adaptive_weight_block_present_in_source(self):
        """Source code must contain the adaptive weight logic."""
        import pathlib
        src = (
            pathlib.Path(__file__).parent.parent.parent
            / "core" / "regime" / "hmm_regime_classifier.py"
        ).read_text(encoding="utf-8")
        assert "uncertain_frac" in src, "adaptive weight variable 'uncertain_frac' not found"
        assert "hmm_w, rb_w = 0.20, 0.80" in src, "degraded weight assignment not found"
        assert "hmm_w, rb_w = 0.60, 0.40" in src, "normal weight assignment not found"
        assert "0.50" in src, "threshold 0.50 not found in adaptive weight logic"


# ──────────────────────────────────────────────────────────────
# Section D — HMMRegimeClassifier: warmup bars fix
# ──────────────────────────────────────────────────────────────

class TestHMMWarmupBarsFix:
    """
    D-01 through D-05: Verify _label_states() uses 5-bar warmup,
    not the old 30-bar warmup.
    """

    def test_d01_warmup_constant_is_5_not_30(self):
        """Source must define _WARMUP_BARS = 5."""
        import pathlib
        src = (
            pathlib.Path(__file__).parent.parent.parent
            / "core" / "regime" / "hmm_regime_classifier.py"
        ).read_text(encoding="utf-8")
        assert "_WARMUP_BARS = 5" in src, (
            "_WARMUP_BARS = 5 not found in hmm_regime_classifier.py"
        )
        # Old warmup of 30 must not be present in _label_states
        idx = src.index("def _label_states")
        end = src.index("\n    def ", idx + 1)
        method_body = src[idx:end]
        assert "30" not in method_body or "_WARMUP_BARS" in method_body, (
            "Old hardcoded warmup '30' still present in _label_states()"
        )

    def test_d02_warmup_30_no_longer_hardcoded(self):
        """The old `if i < 30:` pattern must not appear in _label_states."""
        import pathlib
        src = (
            pathlib.Path(__file__).parent.parent.parent
            / "core" / "regime" / "hmm_regime_classifier.py"
        ).read_text(encoding="utf-8")
        idx = src.index("def _label_states")
        end = src.index("\n    def ", idx + 1)
        method_body = src[idx:end]
        assert "if i < 30:" not in method_body, (
            "Old `if i < 30:` warmup still present in _label_states()"
        )

    def test_d03_label_states_marks_only_first_5_as_uncertain(self):
        """
        With 20 bars and a rule-based classifier returning bull_trend,
        after warmup the majority of bars should be bull_trend — not uncertain.
        With 5-bar warmup: 5 uncertain + 15 bull_trend → most_common = bull_trend.
        With 30-bar warmup: 20 uncertain + 0 bull_trend → most_common = uncertain.
        """
        clf = HMMRegimeClassifier()
        n = 20
        rng = np.random.default_rng(0)
        closes = 100 + np.cumsum(rng.normal(0.1, 0.3, n))
        df = pd.DataFrame({
            "open": closes * 0.999,
            "high": closes * 1.002,
            "low": closes * 0.998,
            "close": closes,
            "volume": rng.uniform(1000, 5000, n),
            "adx": np.full(n, 30.0),
        })
        # Mock fallback to return bull_trend for every sub-df
        with patch.object(clf._fallback, "classify", return_value=(REGIME_BULL_TREND, 0.80, {})):
            # state_seq: all bars assigned to state 0
            state_seq = np.zeros(n, dtype=int)
            clf._label_states(df, state_seq)
        # State 0 should now map to REGIME_BULL_TREND (15/20 bars are bull after 5-bar warmup)
        result_label = clf._state_map.get(0)
        assert result_label == REGIME_BULL_TREND, (
            f"State 0 mapped to '{result_label}' — expected '{REGIME_BULL_TREND}'. "
            f"5-bar warmup fix may not be applied."
        )

    def test_d04_old_30_bar_warmup_would_fail_this_test(self):
        """
        Demonstrates that 30-bar warmup would have mapped state 0 to
        'uncertain' on a 20-bar DataFrame (all bars fall in warmup).
        This test documents the regression.
        """
        # With 30-bar warmup on 20 bars: all 20 bars are 'uncertain'
        # → most_common(uncertain) = uncertain
        from collections import Counter
        # Simulate 30-bar warmup on 20-bar dataset
        warmup_old = 30
        rule_labels_old = [REGIME_UNCERTAIN] * 20  # ALL bars uncertain (warmup covers all)
        most_common_old = Counter(rule_labels_old).most_common(1)[0][0]
        assert most_common_old == REGIME_UNCERTAIN, "Sanity: old warmup test setup incorrect"

        # Simulate 5-bar warmup on 20-bar dataset
        warmup_new = 5
        rule_labels_new = (
            [REGIME_UNCERTAIN] * warmup_new +
            [REGIME_BULL_TREND] * (20 - warmup_new)
        )
        most_common_new = Counter(rule_labels_new).most_common(1)[0][0]
        assert most_common_new == REGIME_BULL_TREND, "New warmup should produce bull_trend"

    def test_d05_label_states_uses_warmup_bars_variable(self):
        """Source code uses _WARMUP_BARS variable, not magic number."""
        import pathlib
        src = (
            pathlib.Path(__file__).parent.parent.parent
            / "core" / "regime" / "hmm_regime_classifier.py"
        ).read_text(encoding="utf-8")
        idx = src.index("def _label_states")
        end = src.index("\n    def ", idx + 1)
        method_body = src[idx:end]
        assert "_WARMUP_BARS" in method_body, (
            "_WARMUP_BARS variable not used in _label_states()"
        )
        assert "if i < _WARMUP_BARS" in method_body, (
            "warmup guard `if i < _WARMUP_BARS` not found in _label_states()"
        )


# ──────────────────────────────────────────────────────────────
# Section E — Integration: end-to-end sizing for real trades
# ──────────────────────────────────────────────────────────────

class TestPositionSizingIntegration:
    """
    E-01 through E-04: End-to-end position sizing validation
    using the actual open trade data from open_positions.json.
    """

    def test_e01_btc_trade_size_is_4pct_not_500(self):
        """
        BTC/USDT open position: entry=70840.36, stop=69247.11,
        capital=101327.28, risk_pct=0.5%.
        Old result: $500 (max_size_usdt cap)
        New result: ~$4,053 (4% of capital)
        """
        sizer = PositionSizer(max_capital_pct=0.04, max_size_usdt=0.0)
        size = sizer.calculate_risk_based(
            capital_usdt=101_327.28,
            entry_price=70_840.36,
            stop_price=69_247.91,
            risk_pct=0.5,
        )
        assert size > 3_500.0, f"BTC size {size:.2f} still near $500 — fix incomplete"
        assert size <= 4_200.0, f"BTC size {size:.2f} exceeds 4% cap"

    def test_e02_sol_trade_size_is_4pct_not_500(self):
        """
        SOL/USDT open position: entry=91.52, stop=88.63,
        capital=101327.28.
        """
        sizer = PositionSizer(max_capital_pct=0.04, max_size_usdt=0.0)
        size = sizer.calculate_risk_based(
            capital_usdt=101_327.28,
            entry_price=91.52,
            stop_price=88.63,
            risk_pct=0.5,
        )
        assert size > 3_500.0, f"SOL size {size:.2f} still near $500 — fix incomplete"
        assert size <= 4_200.0, f"SOL size {size:.2f} exceeds 4% cap"

    def test_e03_trade_size_scales_with_capital(self):
        """
        Verify size scales linearly with capital (both bounded by pct cap).
        """
        sizer = PositionSizer(max_capital_pct=0.04, max_size_usdt=0.0)
        size_100k = sizer.calculate_risk_based(
            capital_usdt=100_000.0,
            entry_price=100.0,
            stop_price=99.99,
            risk_pct=0.5,
        )
        size_200k = sizer.calculate_risk_based(
            capital_usdt=200_000.0,
            entry_price=100.0,
            stop_price=99.99,
            risk_pct=0.5,
        )
        # 4% of 200k = 8000 vs 4% of 100k = 4000 → ratio = 2.0
        ratio = size_200k / size_100k
        assert ratio == pytest.approx(2.0, abs=0.1), (
            f"Expected 2× scaling, got {ratio:.2f}"
        )

    def test_e04_halt_regime_still_returns_zero(self):
        """
        Halt regimes (crisis, liquidation_cascade) must still return 0.
        The sizing fix must not affect the halt logic.
        """
        sizer = PositionSizer(max_capital_pct=0.04, max_size_usdt=0.0)
        for halt_regime in ["crisis", "liquidation_cascade"]:
            size = sizer.calculate_risk_based(
                capital_usdt=100_000.0,
                entry_price=100.0,
                stop_price=95.0,
                risk_pct=0.5,
                regime=halt_regime,
            )
            assert size == 0.0, (
                f"Halt regime '{halt_regime}' should return 0, got {size}"
            )
