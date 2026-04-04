# ============================================================
# NEXUS TRADER — Recommendation Policy (Phase 2)
#
# Defines all recommendation templates keyed by recommendation ID.
# Each template specifies:
#   • action            — concrete operator instruction
#   • rationale         — why this helps
#   • affected_subsystem — what part of NexusTrader to change
#   • priority          — high | medium | low
#   • auto_tune_safe    — True = may be applied without human review
#   • tuning_parameter  — specific parameter to change (if applicable)
#   • tuning_direction  — "increase" | "decrease" | "add_filter" | "manual"
#   • min_trades_required — minimum feedback count before proposing
#
# Deterministic linkage: root_cause → recommendation_id → TuningProposal
# ============================================================
from __future__ import annotations

from typing import Any

# ── All recommendation IDs ────────────────────────────────────
REC_REQUIRE_REGIME_CONFIRMATION     = "REC_REQUIRE_REGIME_CONFIRMATION"
REC_RAISE_REGIME_CONFIDENCE_THRESHOLD = "REC_RAISE_REGIME_CONFIDENCE_THRESHOLD"
REC_RAISE_MIN_CONFLUENCE            = "REC_RAISE_MIN_CONFLUENCE"
REC_REQUIRE_MULTI_MODEL             = "REC_REQUIRE_MULTI_MODEL"
REC_BLOCK_SINGLE_MODEL_SETUPS       = "REC_BLOCK_SINGLE_MODEL_SETUPS"
REC_ENFORCE_MIN_RR                  = "REC_ENFORCE_MIN_RR"
REC_WIDEN_TAKE_PROFIT               = "REC_WIDEN_TAKE_PROFIT"
REC_MANDATORY_STOP_LOSS             = "REC_MANDATORY_STOP_LOSS"
REC_TIGHTEN_ATR_STOP_MULTIPLIER     = "REC_TIGHTEN_ATR_STOP_MULTIPLIER"
REC_WIDEN_ATR_STOP_MULTIPLIER       = "REC_WIDEN_ATR_STOP_MULTIPLIER"
REC_REQUIRE_TAKE_PROFIT             = "REC_REQUIRE_TAKE_PROFIT"
REC_HOLD_TO_TARGET                  = "REC_HOLD_TO_TARGET"
REC_PARTIAL_CLOSE_STRATEGY          = "REC_PARTIAL_CLOSE_STRATEGY"
REC_TRUST_SYSTEM_STOPS              = "REC_TRUST_SYSTEM_STOPS"
REC_REVIEW_MANUAL_CLOSE_POLICY      = "REC_REVIEW_MANUAL_CLOSE_POLICY"
REC_ADD_MIN_HOLD_DURATION           = "REC_ADD_MIN_HOLD_DURATION"
REC_FILTER_NOISE_ENTRIES            = "REC_FILTER_NOISE_ENTRIES"
REC_BLOCK_COUNTER_REGIME            = "REC_BLOCK_COUNTER_REGIME"
REC_ADD_TRAILING_STOP               = "REC_ADD_TRAILING_STOP"
REC_REVIEW_EXIT_TIMING              = "REC_REVIEW_EXIT_TIMING"
REC_ENFORCE_HARD_GATE               = "REC_ENFORCE_HARD_GATE"
REC_BLOCK_CONFLICTED_SIGNALS        = "REC_BLOCK_CONFLICTED_SIGNALS"


RECOMMENDATION_POLICY: dict[str, dict[str, Any]] = {

    REC_REQUIRE_REGIME_CONFIRMATION: {
        "action": (
            "Require that the detected regime matches the trade direction before allowing entry."
            " Uncertain or neutral regime should add a confluence penalty of at least 0.05."
        ),
        "rationale": (
            "Trading against or in an uncertain regime consistently underperforms."
            " Regime confirmation is a low-cost filter with high signal quality."
        ),
        "affected_subsystem": "ConfluenceScorer (REGIME_AFFINITY matrix)",
        "priority": "high",
        "auto_tune_safe": True,
        "tuning_parameter": "REGIME_AFFINITY.uncertain_penalty",
        "tuning_direction": "increase",
        "min_trades_required": 10,
    },

    REC_RAISE_REGIME_CONFIDENCE_THRESHOLD: {
        "action": (
            "Increase the minimum HMM regime confidence required for a TRENDING state"
            " to qualify as regime-confirmed. Suggested: raise from current level by 0.05."
        ),
        "rationale": (
            "Low-confidence regime labels are noisy and produce incorrect direction bias."
            " Tightening the threshold reduces mislabelled regime trades."
        ),
        "affected_subsystem": "HMMRegimeClassifier (min_hmm_confidence)",
        "priority": "medium",
        "auto_tune_safe": True,
        "tuning_parameter": "hmm_classifier.min_confidence",
        "tuning_direction": "increase",
        "min_trades_required": 15,
    },

    REC_RAISE_MIN_CONFLUENCE: {
        "action": (
            "Raise the minimum_confluence_score threshold by 0.03–0.05."
            " Current production value: 0.45."
            " Recommended target: 0.48–0.50 if low-confluence trades underperform."
        ),
        "rationale": (
            "Trades entering below 0.60 confluence show materially lower win rates."
            " Raising the threshold sacrifices trade frequency for quality."
        ),
        "affected_subsystem": "ConfluenceScorer (min_confluence_score)",
        "priority": "high",
        "auto_tune_safe": True,
        "tuning_parameter": "idss.min_confluence_score",
        "tuning_direction": "increase",
        "min_trades_required": 20,
    },

    REC_REQUIRE_MULTI_MODEL: {
        "action": (
            "Block entries where fewer than 2 signal models have fired."
            " Single-model entries should require a confluence bonus of +0.10 to compensate."
        ),
        "rationale": (
            "Single-model setups lack cross-confirmation and produce higher false-signal rates."
            " Requiring multi-model agreement is a well-validated filter in algorithmic trading."
        ),
        "affected_subsystem": "ConfluenceScorer / RiskGate",
        "priority": "high",
        "auto_tune_safe": True,
        "tuning_parameter": "confluence_scorer.min_models_fired",
        "tuning_direction": "increase",
        "min_trades_required": 15,
    },

    REC_BLOCK_SINGLE_MODEL_SETUPS: {
        "action": (
            "Add a hard filter in the RiskGate that rejects entries where only one model fired,"
            " unless confluence is above 0.70 (compensatory strength from a single strong model)."
        ),
        "rationale": (
            "Single-model setups with borderline confluence are the most common source of bad trades."
            " A hard block is more robust than a penalty."
        ),
        "affected_subsystem": "RiskGate",
        "priority": "medium",
        "auto_tune_safe": False,
        "tuning_parameter": "risk_gate.min_models_for_entry",
        "tuning_direction": "add_filter",
        "min_trades_required": 20,
    },

    REC_ENFORCE_MIN_RR: {
        "action": (
            "Ensure the RiskGate hard-rejects entries where theoretical R:R < 1.0."
            " This is already a hard override — verify it is enforced end-to-end."
        ),
        "rationale": (
            "Trades with R:R < 1 have negative expectancy even with a 60% win rate."
            " No parameter tuning compensates for structural negative expectancy."
        ),
        "affected_subsystem": "RiskGate (ev_gate)",
        "priority": "high",
        "auto_tune_safe": False,
        "tuning_parameter": "risk_gate.min_rr",
        "tuning_direction": "manual",
        "min_trades_required": 5,
    },

    REC_WIDEN_TAKE_PROFIT: {
        "action": (
            "Review take-profit placement logic for the models generating poor R:R."
            " Consider extending TP targets using ATR multipliers of 2.5–3.0 instead of 2.0."
        ),
        "rationale": (
            "Tight take-profit levels reduce theoretical R:R and result in frequent premature exits."
            " Wider targets improve trade structure at the cost of lower individual hit rates."
        ),
        "affected_subsystem": "sub_models REGIME_ATR_MULTIPLIERS",
        "priority": "medium",
        "auto_tune_safe": True,
        "tuning_parameter": "sub_model.tp_atr_multiplier",
        "tuning_direction": "increase",
        "min_trades_required": 25,
    },

    REC_MANDATORY_STOP_LOSS: {
        "action": (
            "Verify that PaperExecutor enforces a stop-loss on every new position."
            " If stop_loss is None at entry, reject the trade immediately."
        ),
        "rationale": (
            "A position without a stop-loss exposes the account to unlimited downside."
            " This is a critical system-integrity issue, not a tuning issue."
        ),
        "affected_subsystem": "PaperExecutor / RiskGate",
        "priority": "high",
        "auto_tune_safe": False,
        "tuning_parameter": None,
        "tuning_direction": "manual",
        "min_trades_required": 1,
    },

    REC_TIGHTEN_ATR_STOP_MULTIPLIER: {
        "action": (
            "Reduce the ATR multiplier used to set stop-loss distances."
            " If current multiplier is > 2.0, try 1.5–1.8 for the relevant model/regime combination."
        ),
        "rationale": (
            "Wide stops increase the dollar risk per trade and may result in worse expectancy"
            " than tighter stops with correspondingly tighter targets."
        ),
        "affected_subsystem": "sub_models REGIME_ATR_MULTIPLIERS",
        "priority": "medium",
        "auto_tune_safe": True,
        "tuning_parameter": "sub_model.stop_atr_multiplier",
        "tuning_direction": "decrease",
        "min_trades_required": 25,
    },

    REC_WIDEN_ATR_STOP_MULTIPLIER: {
        "action": (
            "Increase the ATR multiplier for stop-loss placement to reduce premature exits."
            " If current multiplier is < 1.0, increase to 1.2–1.5."
        ),
        "rationale": (
            "Stops placed within market noise will be hit frequently without the"
            " position being directionally wrong. Widening by 0.3–0.5 ATR reduces whipsaws."
        ),
        "affected_subsystem": "sub_models REGIME_ATR_MULTIPLIERS",
        "priority": "medium",
        "auto_tune_safe": True,
        "tuning_parameter": "sub_model.stop_atr_multiplier",
        "tuning_direction": "increase",
        "min_trades_required": 25,
    },

    REC_REQUIRE_TAKE_PROFIT: {
        "action": (
            "Verify that every entry includes a computed take-profit level."
            " Positions without TP should default to a 2.0× ATR target."
        ),
        "rationale": (
            "Without a take-profit, the system has no defined exit strategy."
            " This produces unpredictable exit behaviour and distorted R-multiples."
        ),
        "affected_subsystem": "sub_models / RiskGate",
        "priority": "medium",
        "auto_tune_safe": False,
        "tuning_parameter": None,
        "tuning_direction": "manual",
        "min_trades_required": 5,
    },

    REC_HOLD_TO_TARGET: {
        "action": (
            "Review and reduce operator manual-close intervention on running positions."
            " System-managed exits should be the default unless a structural change has occurred."
        ),
        "rationale": (
            "Premature exits reduce the average R-multiple and distort the expected value"
            " of the strategy. Holding to pre-defined targets is systematically superior."
        ),
        "affected_subsystem": "Operator process",
        "priority": "medium",
        "auto_tune_safe": False,
        "tuning_parameter": None,
        "tuning_direction": "manual",
        "min_trades_required": 10,
    },

    REC_PARTIAL_CLOSE_STRATEGY: {
        "action": (
            "Implement a partial-close protocol: close 50% at 1R profit, trail stop to"
            " breakeven on remainder. This locks in partial profit while preserving upside."
        ),
        "rationale": (
            "Partial closes reduce the emotional pressure to exit prematurely while"
            " capturing partial profit. Operationally well-supported in paper trading."
        ),
        "affected_subsystem": "PaperExecutor",
        "priority": "low",
        "auto_tune_safe": False,
        "tuning_parameter": None,
        "tuning_direction": "manual",
        "min_trades_required": 30,
    },

    REC_TRUST_SYSTEM_STOPS: {
        "action": (
            "Avoid closing positions manually at a loss when the system stop has not been hit."
            " Manual closes at loss below -1% indicate emotional intervention."
        ),
        "rationale": (
            "System stops are set based on structural levels. Manual closes at arbitrary"
            " loss thresholds add noise and reduce expectancy versus the backtested baseline."
        ),
        "affected_subsystem": "Operator process",
        "priority": "high",
        "auto_tune_safe": False,
        "tuning_parameter": None,
        "tuning_direction": "manual",
        "min_trades_required": 5,
    },

    REC_REVIEW_MANUAL_CLOSE_POLICY: {
        "action": (
            "Establish a formal operator policy: manual closes are only permitted if"
            " (a) a hard news event has occurred, or (b) the regime has shifted by two tiers."
        ),
        "rationale": (
            "Without a policy, manual closes are driven by sentiment rather than evidence."
            " A formal rule reduces discretionary noise."
        ),
        "affected_subsystem": "Operator process",
        "priority": "medium",
        "auto_tune_safe": False,
        "tuning_parameter": None,
        "tuning_direction": "manual",
        "min_trades_required": 10,
    },

    REC_ADD_MIN_HOLD_DURATION: {
        "action": (
            "Add a minimum hold duration filter: reject entries that exit within 5 minutes"
            " of opening by detecting rapid reversals before execution."
            " Alternatively, add a 5-minute entry confirmation bar."
        ),
        "rationale": (
            "Very short trades indicate the entry was on market noise."
            " A minimum hold period or confirmation bar reduces whipsaw entries."
        ),
        "affected_subsystem": "ConfluenceScorer / RiskGate",
        "priority": "medium",
        "auto_tune_safe": True,
        "tuning_parameter": "risk_gate.min_entry_confirmation_bars",
        "tuning_direction": "increase",
        "min_trades_required": 15,
    },

    REC_FILTER_NOISE_ENTRIES: {
        "action": (
            "Add an ATR-normalised volatility filter: require that recent 5-bar range"
            " is within 0.5–2.0× ATR before allowing entry, to avoid choppy-market entries."
        ),
        "rationale": (
            "Entries during unusually low or high volatility produce worse expectancy."
            " A volatility band filter reduces noise entries systematically."
        ),
        "affected_subsystem": "RiskGate / ConfluenceScorer",
        "priority": "medium",
        "auto_tune_safe": True,
        "tuning_parameter": "risk_gate.atr_volatility_filter",
        "tuning_direction": "add_filter",
        "min_trades_required": 20,
    },

    REC_BLOCK_COUNTER_REGIME: {
        "action": (
            "Enforce a hard block on entries where regime affinity == -1 (counter-regime)."
            " Counter-regime entries should require confluence >= 0.75 or be blocked entirely."
        ),
        "rationale": (
            "Counter-regime entries have the highest false-positive rate in backtesting."
            " The regime signal is specifically designed to prevent this class of trade."
        ),
        "affected_subsystem": "ConfluenceScorer / RiskGate",
        "priority": "high",
        "auto_tune_safe": True,
        "tuning_parameter": "confluence_scorer.counter_regime_block_threshold",
        "tuning_direction": "add_filter",
        "min_trades_required": 10,
    },

    REC_ADD_TRAILING_STOP: {
        "action": (
            "Implement a trailing stop that moves to breakeven after 1R profit is achieved."
            " This protects against held-through-reversal losses on high-confluence trades."
        ),
        "rationale": (
            "High-confluence trades that reversed into a large loss represent the most"
            " avoidable category of bad outcomes. A trailing stop would have preserved"
            " capital in most such cases."
        ),
        "affected_subsystem": "PaperExecutor",
        "priority": "medium",
        "auto_tune_safe": False,
        "tuning_parameter": None,
        "tuning_direction": "manual",
        "min_trades_required": 15,
    },

    REC_REVIEW_EXIT_TIMING: {
        "action": (
            "Audit the exit timing of held-through-reversal trades."
            " Check if exit signals from secondary models were firing before the reversal"
            " point and could serve as early-warning exit triggers."
        ),
        "rationale": (
            "Market reversals often produce warning signals (e.g. momentum divergence,"
            " volume spike, regime shift) before the full reversal."
            " Using these as soft exits can reduce average loss on reversals."
        ),
        "affected_subsystem": "PaperExecutor / SignalGenerator exit logic",
        "priority": "low",
        "auto_tune_safe": False,
        "tuning_parameter": None,
        "tuning_direction": "manual",
        "min_trades_required": 20,
    },

    REC_ENFORCE_HARD_GATE: {
        "action": (
            "Audit the ConfluenceScorer and RiskGate to confirm that the minimum confluence gate"
            " (0.45) is enforced without bypass at every code path that can trigger an entry."
        ),
        "rationale": (
            "Below-minimum-confluence trades indicate the gate may be bypassable in certain"
            " code paths (e.g. manual override, test mode). Every entry must be gated."
        ),
        "affected_subsystem": "RiskGate / ConfluenceScorer",
        "priority": "high",
        "auto_tune_safe": False,
        "tuning_parameter": None,
        "tuning_direction": "manual",
        "min_trades_required": 1,
    },

    REC_BLOCK_CONFLICTED_SIGNALS: {
        "action": (
            "Add a signal conflict threshold: if model disagreement exceeds 50%"
            " (models_long ÷ total_models < 0.5 or > 0.5 ambiguously), block the entry."
        ),
        "rationale": (
            "High model conflict indicates the signal environment is ambiguous."
            " Blocking conflicted entries avoids low-probability setups."
        ),
        "affected_subsystem": "ConfluenceScorer",
        "priority": "medium",
        "auto_tune_safe": True,
        "tuning_parameter": "confluence_scorer.max_signal_conflict_pct",
        "tuning_direction": "add_filter",
        "min_trades_required": 15,
    },
}


def get_recommendation(rec_id: str) -> dict:
    """Return recommendation template dict, or empty dict if not found."""
    return RECOMMENDATION_POLICY.get(rec_id, {})


def build_recommendation_object(rec_id: str, root_cause_category: str) -> dict:
    """
    Build a full recommendation dict for use in analysis output.
    Merges the policy template with the linked root cause category.
    """
    tmpl = RECOMMENDATION_POLICY.get(rec_id, {})
    if not tmpl:
        return {}
    return {
        "rec_id":           rec_id,
        "category":         root_cause_category,
        "action":           tmpl.get("action", ""),
        "rationale":        tmpl.get("rationale", ""),
        "affected_subsystem": tmpl.get("affected_subsystem", ""),
        "priority":         tmpl.get("priority", "medium"),
        "auto_tune_safe":   tmpl.get("auto_tune_safe", False),
        "tuning_parameter": tmpl.get("tuning_parameter"),
        "tuning_direction": tmpl.get("tuning_direction", "manual"),
        "min_trades_required": tmpl.get("min_trades_required", 10),
    }
