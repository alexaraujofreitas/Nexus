# ============================================================
# NEXUS TRADER — Root Cause Analyzer
#
# Maps scoring penalty_log entries to structured root-cause
# objects. Each root cause has:
#   code        : str  — machine-readable identifier
#   category    : str  — one of 15 canonical categories
#   description : str  — human-readable explanation
#   severity    : str  — "critical" | "major" | "minor"
#   dimension   : str  — "setup" | "risk" | "execution" | "decision"
# ============================================================
from __future__ import annotations

from typing import Optional

# ── 15 canonical root-cause categories ───────────────────────
CAT_REGIME_MISMATCH     = "REGIME_MISMATCH"
CAT_LOW_CONFLUENCE      = "LOW_CONFLUENCE"
CAT_WEAK_MODELS         = "WEAK_MODELS"
CAT_POOR_RR             = "POOR_RR"
CAT_NO_STOP             = "NO_STOP_LOSS"
CAT_WIDE_STOP           = "WIDE_STOP"
CAT_TIGHT_STOP          = "TIGHT_STOP"
CAT_NO_TARGET           = "NO_TARGET"
CAT_PREMATURE_EXIT      = "PREMATURE_EXIT"
CAT_MANUAL_LOSS         = "MANUAL_LOSS_OVERRIDE"
CAT_NOISE_TRADE         = "NOISE_TRADE"
CAT_COUNTER_REGIME      = "COUNTER_REGIME_ENTRY"
CAT_HELD_REVERSAL       = "HELD_THROUGH_REVERSAL"
CAT_BELOW_MIN_CONF      = "BELOW_MINIMUM_CONFLUENCE"
CAT_NO_MODEL_AGREEMENT  = "NO_MODEL_AGREEMENT"


# ── Penalty code → root-cause mapping ────────────────────────
# Each entry: (penalty_prefix, category, severity, human_description)
_PENALTY_MAP: list[tuple[str, str, str, str]] = [
    # Setup
    ("COUNTER_REGIME:",     CAT_COUNTER_REGIME,    "critical",
     "Trade direction conflicts with the current market regime"),
    ("UNCERTAIN_REGIME",    CAT_REGIME_MISMATCH,   "major",
     "Regime was 'uncertain' at entry — low-confidence market state"),
    ("NEUTRAL_REGIME:",     CAT_REGIME_MISMATCH,   "minor",
     "Regime provided neutral (not aligned) support for this direction"),
    ("BELOW_MIN_CONFLUENCE:", CAT_BELOW_MIN_CONF,  "critical",
     "Confluence score was below the minimum system gate threshold"),
    ("LOW_CONFLUENCE:",     CAT_LOW_CONFLUENCE,    "major",
     "Confluence score was low — signal agreement was below ideal"),
    ("MODERATE_CONFLUENCE:", CAT_LOW_CONFLUENCE,   "minor",
     "Confluence score was moderate — signal agreement could be stronger"),
    ("NO_MODELS_FIRED",     CAT_NO_MODEL_AGREEMENT,"critical",
     "No signal models fired at entry — no technical basis for the trade"),
    ("SINGLE_MODEL_ONLY",   CAT_WEAK_MODELS,       "major",
     "Only one signal model fired — insufficient multi-model confirmation"),

    # Risk
    ("NO_STOP_LOSS",        CAT_NO_STOP,           "critical",
     "No stop-loss was set — risk is unlimited and hard override triggered"),
    ("NO_TAKE_PROFIT",      CAT_NO_TARGET,         "major",
     "No take-profit target was set — no defined exit for profit capture"),
    ("RR_BELOW_FLOOR:",     CAT_POOR_RR,           "critical",
     "Theoretical R:R is below the 1:1 minimum floor — negative expectancy setup"),
    ("LOW_RR:",             CAT_POOR_RR,           "major",
     "Theoretical R:R is below the 1.5 preferred threshold"),
    ("RR_UNCOMPUTABLE",     CAT_POOR_RR,           "minor",
     "R:R could not be computed — missing price data for entry/stop/target"),

    # Execution
    ("NOISE_TRADE:",        CAT_NOISE_TRADE,       "major",
     "Trade duration was very short (<5 min) — likely entered on noise"),
    ("MANUAL_CLOSE_AT_LOSS", CAT_MANUAL_LOSS,      "major",
     "Trade was closed manually while in a loss — bypassed stop management"),
    ("MANUAL_CLOSE",        CAT_PREMATURE_EXIT,    "minor",
     "Trade was closed manually — system stop/target management bypassed"),
    ("PREMATURE_EXIT:",     CAT_PREMATURE_EXIT,    "minor",
     "Trade was closed before the take-profit zone was reached"),

    # Decision
    ("TOOK_TRADE_BELOW_CONFLUENCE:", CAT_BELOW_MIN_CONF, "critical",
     "Trade was entered despite confluence being below minimum threshold"),
    ("LOW_CONFLUENCE_DECISION:",     CAT_LOW_CONFLUENCE, "major",
     "Entry decision was made with low confluence — below preferred level"),
    ("COUNTER_REGIME_DECISION:",     CAT_COUNTER_REGIME, "critical",
     "Entry decision was made against the current regime direction"),
    ("MANUAL_LOSS_OVERRIDE:",        CAT_MANUAL_LOSS,   "major",
     "Significant manual loss — position closed at large loss without SL trigger"),
    ("HELD_THROUGH_REVERSAL:",       CAT_HELD_REVERSAL, "major",
     "High-confluence trade that reversed — possible market-condition change"),
    ("STOP_HIT_COUNTER_REGIME_ENTRY:", CAT_COUNTER_REGIME, "major",
     "Stop-loss was hit on a counter-regime entry — regime was the root cause"),
    ("DECISION_WITHOUT_MODELS:",     CAT_WEAK_MODELS,   "critical",
     "Decision made without model signal data — no quantitative basis"),
]


def _match_penalty(penalty_code: str) -> Optional[tuple[str, str, str]]:
    """
    Returns (category, severity, description) for the first matching rule.
    """
    for prefix, cat, sev, desc in _PENALTY_MAP:
        if penalty_code.startswith(prefix) or penalty_code == prefix.rstrip(":"):
            return cat, sev, desc
    return None


def analyze_root_causes(scoring_result: dict) -> list[dict]:
    """
    Convert scoring penalty_log into a deduplicated list of root-cause objects.

    Each root-cause dict:
    {
        "code":        str,   # unique penalty code
        "category":    str,   # one of 15 canonical categories
        "severity":    str,   # critical | major | minor
        "dimension":   str,   # setup | risk | execution | decision
        "description": str,
    }

    Root causes are sorted by severity (critical first).
    """
    penalty_log: dict[str, list[str]] = scoring_result.get("penalty_log", {})
    seen_categories: set[str] = set()
    root_causes: list[dict] = []

    _severity_order = {"critical": 0, "major": 1, "minor": 2}

    for dimension, codes in penalty_log.items():
        for code in codes:
            match = _match_penalty(code)
            if match is None:
                continue
            category, severity, description = match

            # Deduplicate by category (don't show the same root cause twice
            # from different dimensions)
            if category in seen_categories:
                continue
            seen_categories.add(category)

            root_causes.append({
                "code":        code,
                "category":    category,
                "severity":    severity,
                "dimension":   dimension,
                "description": description,
            })

    # Sort: critical → major → minor, then by dimension
    root_causes.sort(
        key=lambda rc: (_severity_order.get(rc["severity"], 9), rc["dimension"])
    )

    return root_causes
