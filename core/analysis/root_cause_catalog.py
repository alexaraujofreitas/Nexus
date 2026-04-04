# ============================================================
# NEXUS TRADER — Root Cause Catalog (Phase 2)
#
# Formal catalog for all 15 canonical root-cause categories.
# Each entry defines:
#   • detection_rule      — what evidence triggers this cause
#   • evidence_fields     — trade dict keys used for detection
#   • severity_mapping    — conditions → severity levels
#   • recommendation_ids  — recommendation templates to apply
#   • auto_tune_eligible  — safe for automated parameter change
#   • description         — human-readable explanation
#
# Used by:
#   • root_cause_analyzer.py   — penalty → root cause mapping
#   • recommendation_policy.py — root cause → recommendation
#   • tuning_proposal_generator.py — recurring → tuning proposal
# ============================================================
from __future__ import annotations

from typing import Any

# ── 15 canonical root-cause categories ───────────────────────
RC_REGIME_MISMATCH          = "REGIME_MISMATCH"
RC_LOW_CONFLUENCE           = "LOW_CONFLUENCE"
RC_WEAK_MODELS              = "WEAK_MODELS"
RC_POOR_RR                  = "POOR_RR"
RC_NO_STOP_LOSS             = "NO_STOP_LOSS"
RC_WIDE_STOP                = "WIDE_STOP"
RC_TIGHT_STOP               = "TIGHT_STOP"
RC_NO_TARGET                = "NO_TARGET"
RC_PREMATURE_EXIT           = "PREMATURE_EXIT"
RC_MANUAL_LOSS_OVERRIDE     = "MANUAL_LOSS_OVERRIDE"
RC_NOISE_TRADE              = "NOISE_TRADE"
RC_COUNTER_REGIME_ENTRY     = "COUNTER_REGIME_ENTRY"
RC_HELD_THROUGH_REVERSAL    = "HELD_THROUGH_REVERSAL"
RC_BELOW_MINIMUM_CONFLUENCE = "BELOW_MINIMUM_CONFLUENCE"
RC_NO_MODEL_AGREEMENT       = "NO_MODEL_AGREEMENT"

ALL_ROOT_CAUSE_CATEGORIES: list[str] = [
    RC_REGIME_MISMATCH, RC_LOW_CONFLUENCE, RC_WEAK_MODELS,
    RC_POOR_RR, RC_NO_STOP_LOSS, RC_WIDE_STOP, RC_TIGHT_STOP,
    RC_NO_TARGET, RC_PREMATURE_EXIT, RC_MANUAL_LOSS_OVERRIDE,
    RC_NOISE_TRADE, RC_COUNTER_REGIME_ENTRY, RC_HELD_THROUGH_REVERSAL,
    RC_BELOW_MINIMUM_CONFLUENCE, RC_NO_MODEL_AGREEMENT,
]

# ── Severity constants ────────────────────────────────────────
SEV_CRITICAL = "critical"
SEV_MAJOR    = "major"
SEV_MINOR    = "minor"

_SEV_RANK = {SEV_CRITICAL: 0, SEV_MAJOR: 1, SEV_MINOR: 2}


def severity_rank(sev: str) -> int:
    """Lower number = more severe (critical=0, major=1, minor=2)."""
    return _SEV_RANK.get(sev, 2)


# ── Full catalog ─────────────────────────────────────────────
# Each entry is a dict describing every aspect of a root cause.
ROOT_CAUSE_CATALOG: dict[str, dict[str, Any]] = {

    RC_REGIME_MISMATCH: {
        "description": (
            "The market regime at entry was 'uncertain' or provided no directional"
            " support, increasing the probability of a random outcome."
        ),
        "evidence_fields":  ["regime", "hmm_confidence", "regime_confidence"],
        "severity_default": SEV_MAJOR,
        "severity_rule":    "critical if regime=='uncertain' and models<2; major otherwise",
        "recommendation_ids": [
            "REC_REQUIRE_REGIME_CONFIRMATION",
            "REC_RAISE_REGIME_CONFIDENCE_THRESHOLD",
        ],
        "auto_tune_eligible": True,
        "affected_subsystem": "ConfluenceScorer / RuleBasedRegimeClassifier",
        "penalty_prefixes":   ["UNCERTAIN_REGIME", "NEUTRAL_REGIME:"],
    },

    RC_LOW_CONFLUENCE: {
        "description": (
            "Signal confluence was below the preferred threshold (0.60), meaning"
            " fewer models agreed on the direction at entry."
        ),
        "evidence_fields":  ["score", "confluence_score"],
        "severity_default": SEV_MAJOR,
        "severity_rule":    "critical if score<0.45; major if score<0.60; minor otherwise",
        "recommendation_ids": [
            "REC_RAISE_MIN_CONFLUENCE",
            "REC_REQUIRE_MULTI_MODEL",
        ],
        "auto_tune_eligible": True,
        "affected_subsystem": "ConfluenceScorer",
        "penalty_prefixes":   ["LOW_CONFLUENCE:", "MODERATE_CONFLUENCE:", "LOW_CONFLUENCE_DECISION:"],
    },

    RC_WEAK_MODELS: {
        "description": (
            "Only one signal model (or no models) fired at entry."
            " Insufficient multi-model confirmation increases false-signal risk."
        ),
        "evidence_fields":  ["models_fired"],
        "severity_default": SEV_MAJOR,
        "severity_rule":    "critical if models_fired is empty; major if len==1",
        "recommendation_ids": [
            "REC_REQUIRE_MULTI_MODEL",
            "REC_BLOCK_SINGLE_MODEL_SETUPS",
        ],
        "auto_tune_eligible": True,
        "affected_subsystem": "SignalGenerator / ConfluenceScorer",
        "penalty_prefixes":   ["SINGLE_MODEL_ONLY", "NO_MODELS_FIRED", "DECISION_WITHOUT_MODELS:"],
    },

    RC_POOR_RR: {
        "description": (
            "The theoretical risk-to-reward ratio was below the preferred 1.5:1 threshold."
            " Negative-expectancy setup structure detected."
        ),
        "evidence_fields":  ["entry_price", "stop_loss", "take_profit", "side"],
        "severity_default": SEV_MAJOR,
        "severity_rule":    "critical if rr<1.0 (hard override); major if rr<1.5; minor otherwise",
        "recommendation_ids": [
            "REC_ENFORCE_MIN_RR",
            "REC_WIDEN_TAKE_PROFIT",
        ],
        "auto_tune_eligible": True,
        "affected_subsystem": "RiskGate / PositionSizer",
        "penalty_prefixes":   ["RR_BELOW_FLOOR:", "LOW_RR:", "RR_UNCOMPUTABLE"],
    },

    RC_NO_STOP_LOSS: {
        "description": (
            "No stop-loss was set. This is a hard override condition — risk is"
            " theoretically unlimited. Always classified BAD."
        ),
        "evidence_fields":  ["stop_loss"],
        "severity_default": SEV_CRITICAL,
        "severity_rule":    "always critical",
        "recommendation_ids": [
            "REC_MANDATORY_STOP_LOSS",
        ],
        "auto_tune_eligible": False,
        "affected_subsystem": "RiskGate / PaperExecutor",
        "penalty_prefixes":   ["NO_STOP_LOSS"],
    },

    RC_WIDE_STOP: {
        "description": (
            "Stop-loss is placed unusually wide relative to ATR or entry price,"
            " risking disproportionate capital on a single trade."
        ),
        "evidence_fields":  ["stop_loss", "entry_price", "atr", "side"],
        "severity_default": SEV_MAJOR,
        "severity_rule":    "major if stop_pct > 3*atr; minor if stop_pct > 2*atr",
        "recommendation_ids": [
            "REC_TIGHTEN_ATR_STOP_MULTIPLIER",
        ],
        "auto_tune_eligible": True,
        "affected_subsystem": "RiskGate / sub-model ATR multipliers",
        "penalty_prefixes":   ["WIDE_STOP:"],
    },

    RC_TIGHT_STOP: {
        "description": (
            "Stop-loss is placed tighter than market noise (< 0.5× ATR),"
            " causing premature exits on normal volatility."
        ),
        "evidence_fields":  ["stop_loss", "entry_price", "atr", "side"],
        "severity_default": SEV_MINOR,
        "severity_rule":    "minor if stop_pct < 0.5*atr",
        "recommendation_ids": [
            "REC_WIDEN_ATR_STOP_MULTIPLIER",
        ],
        "auto_tune_eligible": True,
        "affected_subsystem": "RiskGate / sub-model ATR multipliers",
        "penalty_prefixes":   ["TIGHT_STOP:"],
    },

    RC_NO_TARGET: {
        "description": (
            "No take-profit target was set. Without a defined exit for profit capture,"
            " the system relies entirely on manual or volatility-based exits."
        ),
        "evidence_fields":  ["take_profit"],
        "severity_default": SEV_MAJOR,
        "severity_rule":    "major always",
        "recommendation_ids": [
            "REC_REQUIRE_TAKE_PROFIT",
        ],
        "auto_tune_eligible": False,
        "affected_subsystem": "RiskGate / PositionSizer",
        "penalty_prefixes":   ["NO_TAKE_PROFIT"],
    },

    RC_PREMATURE_EXIT: {
        "description": (
            "The trade was closed before reaching the take-profit zone."
            " Premature exits reduce realised R-multiple and distort expectancy."
        ),
        "evidence_fields":  ["exit_reason", "exit_price", "take_profit"],
        "severity_default": SEV_MINOR,
        "severity_rule":    "major if exit < 80% of TP; minor if exit in 80–95% of TP",
        "recommendation_ids": [
            "REC_HOLD_TO_TARGET",
            "REC_PARTIAL_CLOSE_STRATEGY",
        ],
        "auto_tune_eligible": False,
        "affected_subsystem": "PaperExecutor / operator discretion",
        "penalty_prefixes":   ["PREMATURE_EXIT:", "MANUAL_CLOSE"],
    },

    RC_MANUAL_LOSS_OVERRIDE: {
        "description": (
            "The trade was closed manually at a significant loss, bypassing the"
            " system stop-loss. Indicates operator intervention that may reflect"
            " emotional decision-making."
        ),
        "evidence_fields":  ["exit_reason", "pnl_pct", "pnl_usdt"],
        "severity_default": SEV_MAJOR,
        "severity_rule":    "critical if pnl_pct < -2%; major if pnl_pct < -1%",
        "recommendation_ids": [
            "REC_TRUST_SYSTEM_STOPS",
            "REC_REVIEW_MANUAL_CLOSE_POLICY",
        ],
        "auto_tune_eligible": False,
        "affected_subsystem": "Operator process",
        "penalty_prefixes":   ["MANUAL_LOSS_OVERRIDE:", "MANUAL_CLOSE_AT_LOSS"],
    },

    RC_NOISE_TRADE: {
        "description": (
            "Trade duration was extremely short (< 5 minutes), suggesting the entry"
            " was triggered by market noise rather than a structural signal."
        ),
        "evidence_fields":  ["duration_s"],
        "severity_default": SEV_MAJOR,
        "severity_rule":    "major if duration_s < 300",
        "recommendation_ids": [
            "REC_ADD_MIN_HOLD_DURATION",
            "REC_FILTER_NOISE_ENTRIES",
        ],
        "auto_tune_eligible": True,
        "affected_subsystem": "RiskGate / ConfluenceScorer",
        "penalty_prefixes":   ["NOISE_TRADE:"],
    },

    RC_COUNTER_REGIME_ENTRY: {
        "description": (
            "The trade direction was counter to the detected market regime."
            " Entering against the regime significantly reduces win probability."
        ),
        "evidence_fields":  ["regime", "side"],
        "severity_default": SEV_CRITICAL,
        "severity_rule":    "always critical",
        "recommendation_ids": [
            "REC_BLOCK_COUNTER_REGIME",
            "REC_REQUIRE_REGIME_CONFIRMATION",
        ],
        "auto_tune_eligible": True,
        "affected_subsystem": "ConfluenceScorer / RiskGate",
        "penalty_prefixes":   ["COUNTER_REGIME:", "COUNTER_REGIME_DECISION:", "STOP_HIT_COUNTER_REGIME_ENTRY:"],
    },

    RC_HELD_THROUGH_REVERSAL: {
        "description": (
            "A high-confluence trade that closed at a large loss despite good initial"
            " setup — suggesting the market reversed after entry."
            " Possible adverse timing or black-swan event."
        ),
        "evidence_fields":  ["exit_reason", "pnl_pct", "score"],
        "severity_default": SEV_MAJOR,
        "severity_rule":    "major if pnl_pct < -2% and score >= 0.6",
        "recommendation_ids": [
            "REC_ADD_TRAILING_STOP",
            "REC_REVIEW_EXIT_TIMING",
        ],
        "auto_tune_eligible": False,
        "affected_subsystem": "PaperExecutor / exit logic",
        "penalty_prefixes":   ["HELD_THROUGH_REVERSAL:"],
    },

    RC_BELOW_MINIMUM_CONFLUENCE: {
        "description": (
            "Confluence score was below the system's minimum gate threshold (0.45)."
            " This is a hard override condition — the system should have blocked this entry."
        ),
        "evidence_fields":  ["score"],
        "severity_default": SEV_CRITICAL,
        "severity_rule":    "always critical",
        "recommendation_ids": [
            "REC_RAISE_MIN_CONFLUENCE",
            "REC_ENFORCE_HARD_GATE",
        ],
        "auto_tune_eligible": True,
        "affected_subsystem": "ConfluenceScorer / RiskGate",
        "penalty_prefixes":   [
            "BELOW_MIN_CONFLUENCE:", "TOOK_TRADE_BELOW_CONFLUENCE:",
        ],
    },

    RC_NO_MODEL_AGREEMENT: {
        "description": (
            "No signal model fired at entry, or models were in conflict."
            " Without quantitative basis, the trade decision was not system-driven."
        ),
        "evidence_fields":  ["models_fired", "signal_conflict_score"],
        "severity_default": SEV_CRITICAL,
        "severity_rule":    "critical if no models; major if conflict > 50%",
        "recommendation_ids": [
            "REC_REQUIRE_MULTI_MODEL",
            "REC_BLOCK_CONFLICTED_SIGNALS",
        ],
        "auto_tune_eligible": True,
        "affected_subsystem": "SignalGenerator / ConfluenceScorer",
        "penalty_prefixes":   ["NO_MODELS_FIRED", "DECISION_WITHOUT_MODELS:", "SIGNAL_CONFLICT_HIGH:"],
    },
}


# ── Lookup helpers ────────────────────────────────────────────

def get_catalog_entry(category: str) -> dict:
    """Return catalog entry for a category, or empty dict if not found."""
    return ROOT_CAUSE_CATALOG.get(category, {})


def get_recommendation_ids(category: str) -> list[str]:
    """Return list of recommendation IDs for a root-cause category."""
    return ROOT_CAUSE_CATALOG.get(category, {}).get("recommendation_ids", [])


def is_auto_tune_eligible(category: str) -> bool:
    """Return True if this root cause may trigger an auto-tuning proposal."""
    return ROOT_CAUSE_CATALOG.get(category, {}).get("auto_tune_eligible", False)


def get_affected_subsystem(category: str) -> str:
    """Return the affected subsystem name for a root cause."""
    return ROOT_CAUSE_CATALOG.get(category, {}).get("affected_subsystem", "unknown")


def get_penalty_prefixes_for_category(category: str) -> list[str]:
    """Return penalty log prefixes that map to this category."""
    return ROOT_CAUSE_CATALOG.get(category, {}).get("penalty_prefixes", [])
