# ============================================================
# NEXUS TRADER — Canonical Analysis Contract (Phase 3)
#
# Defines the stable field contract for the canonical analysis
# object. All channels (UI, notifications, persistence) must
# produce objects that pass contract validation.
#
# Version history:
#   1.0 — Phase 1: basic scoring + root causes
#   2.0 — Phase 2: thesis, forensics, canonical renderer
# ============================================================
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

VERSION = "2.0"

# ── Required fields ──────────────────────────────────────────
# These MUST be present in every canonical analysis object.
REQUIRED_FIELDS_COMMON = frozenset({
    "overall_score",
    "setup_score",
    "risk_score",
    "execution_score",
    "decision_score",
    "classification",
    "classification_emoji",
    "hard_overrides",
    "penalty_log",
    "root_causes",
    "recommendations",
    "is_open",
    "rr_ratio",
    "regime_affinity",
})

REQUIRED_FIELDS_OPEN = frozenset({
    "thesis",           # open_trade_thesis dict
}) | REQUIRED_FIELDS_COMMON

REQUIRED_FIELDS_CLOSED = frozenset({
    "forensics",        # decision_forensics dict
}) | REQUIRED_FIELDS_COMMON

# ── Required forensics sub-fields ───────────────────────────
REQUIRED_FORENSICS_FIELDS = frozenset({
    "decision_outcome_matrix_label",
    "avoidable_loss_flag",
    "preventability_score",
    "randomness_score",
    "failure_domain_primary",
})

# ── Required thesis sub-fields ───────────────────────────────
REQUIRED_THESIS_FIELDS = frozenset({
    "entry_thesis_summary",
    "thesis_status",
    "live_validity_score",
    "current_disposition",
    "thesis_changed_since_entry",
})

# ── Notification payload required keys ──────────────────────
REQUIRED_NOTIFICATION_KEYS = frozenset({
    "analysis_overall",
    "analysis_classification",
    "analysis_emoji",
    "analysis_rr",
    "analysis_summary_line",
    "analysis_notification_lines",
})

# ── Optional fields (present in full analysis, may be absent in projections) ─
OPTIONAL_FIELDS = frozenset({
    "ai_explanation",
    "regime_confidence_at_entry",
    "htf_confirmed_at_entry",
    "_contract_version",
    "_validated_at",
})

# ── Channel-safe fields (safe to expose to any channel) ─────
CHANNEL_SAFE_FIELDS = frozenset({
    "overall_score", "setup_score", "risk_score", "execution_score",
    "decision_score", "classification", "classification_emoji",
    "hard_overrides", "root_causes", "recommendations",
    "is_open", "rr_ratio", "regime_affinity",
    "ai_explanation",
})

# ── Persistence-safe fields (stored to DB/disk) ──────────────
PERSISTENCE_SAFE_FIELDS = frozenset({
    "overall_score", "setup_score", "risk_score", "execution_score",
    "decision_score", "classification", "hard_overrides",
    "root_causes", "recommendations", "penalty_log",
    "forensics", "regime_confidence_at_entry", "htf_confirmed_at_entry",
    "ai_explanation",
})


class ContractError(ValueError):
    """Raised when an analysis object violates the canonical contract."""
    pass


def validate_open_analysis(d: dict[str, Any]) -> list[str]:
    """
    Validate an open-trade analysis object.
    Returns a list of error strings (empty = valid).
    """
    errors: list[str] = []

    if not isinstance(d, dict):
        return ["analysis object must be a dict"]

    for field in REQUIRED_FIELDS_OPEN:
        if field not in d:
            errors.append(f"missing required field: '{field}'")

    # Validate thesis sub-fields
    thesis = d.get("thesis")
    if thesis is not None:
        if not isinstance(thesis, dict):
            errors.append("'thesis' must be a dict")
        else:
            for field in REQUIRED_THESIS_FIELDS:
                if field not in thesis:
                    errors.append(f"missing thesis sub-field: '{field}'")

    # Score range checks
    for score_field in ("overall_score", "setup_score", "risk_score",
                        "execution_score", "decision_score"):
        val = d.get(score_field)
        if val is not None:
            try:
                fval = float(val)
                if not (0.0 <= fval <= 100.0):
                    errors.append(f"'{score_field}' out of range [0,100]: {fval}")
            except (TypeError, ValueError):
                errors.append(f"'{score_field}' must be numeric, got {type(val).__name__}")

    # Classification must be one of the valid values
    cls = d.get("classification")
    if cls not in (None, "GOOD", "BAD", "NEUTRAL"):
        errors.append(f"invalid classification '{cls}' — must be GOOD/BAD/NEUTRAL")

    return errors


def validate_closed_analysis(d: dict[str, Any]) -> list[str]:
    """
    Validate a closed-trade analysis object.
    Returns a list of error strings (empty = valid).
    """
    errors: list[str] = []

    if not isinstance(d, dict):
        return ["analysis object must be a dict"]

    for field in REQUIRED_FIELDS_CLOSED:
        if field not in d:
            errors.append(f"missing required field: '{field}'")

    # Validate forensics sub-fields
    forensics = d.get("forensics")
    if forensics is not None:
        if not isinstance(forensics, dict):
            errors.append("'forensics' must be a dict")
        else:
            for field in REQUIRED_FORENSICS_FIELDS:
                if field not in forensics:
                    errors.append(f"missing forensics sub-field: '{field}'")

    # Score range checks
    for score_field in ("overall_score", "setup_score", "risk_score",
                        "execution_score", "decision_score"):
        val = d.get(score_field)
        if val is not None:
            try:
                fval = float(val)
                if not (0.0 <= fval <= 100.0):
                    errors.append(f"'{score_field}' out of range [0,100]: {fval}")
            except (TypeError, ValueError):
                errors.append(f"'{score_field}' must be numeric, got {type(val).__name__}")

    # Classification
    cls = d.get("classification")
    if cls not in (None, "GOOD", "BAD", "NEUTRAL"):
        errors.append(f"invalid classification '{cls}' — must be GOOD/BAD/NEUTRAL")

    # Forensics score range
    forensics = d.get("forensics") or {}
    for score_field in ("preventability_score", "randomness_score", "model_conflict_score"):
        val = forensics.get(score_field)
        if val is not None:
            try:
                fval = float(val)
                if not (0.0 <= fval <= 100.0):
                    errors.append(f"forensics.'{score_field}' out of range [0,100]: {fval}")
            except (TypeError, ValueError):
                errors.append(f"forensics.'{score_field}' must be numeric")

    return errors


def validate_notification_payload(d: dict[str, Any]) -> list[str]:
    """
    Validate a notification payload dict.
    Returns a list of error strings (empty = valid).
    """
    errors: list[str] = []

    if not isinstance(d, dict):
        return ["notification payload must be a dict"]

    for key in REQUIRED_NOTIFICATION_KEYS:
        if key not in d:
            errors.append(f"missing notification key: '{key}'")

    lines = d.get("analysis_notification_lines")
    if lines is not None and not isinstance(lines, list):
        errors.append("'analysis_notification_lines' must be a list")

    return errors


def stamp_version(d: dict[str, Any]) -> dict[str, Any]:
    """
    Add contract version and validation timestamp to an analysis dict.
    Returns the same dict (modified in place) for chaining.
    """
    d["_contract_version"] = VERSION
    d["_validated_at"] = datetime.now(timezone.utc).isoformat()
    return d


def assert_valid_open(d: dict[str, Any]) -> None:
    """Raise ContractError if the open-trade analysis is invalid."""
    errors = validate_open_analysis(d)
    if errors:
        raise ContractError(f"Open analysis contract violation: {'; '.join(errors)}")


def assert_valid_closed(d: dict[str, Any]) -> None:
    """Raise ContractError if the closed-trade analysis is invalid."""
    errors = validate_closed_analysis(d)
    if errors:
        raise ContractError(f"Closed analysis contract violation: {'; '.join(errors)}")


def assert_valid_notification(d: dict[str, Any]) -> None:
    """Raise ContractError if the notification payload is invalid."""
    errors = validate_notification_payload(d)
    if errors:
        raise ContractError(f"Notification payload contract violation: {'; '.join(errors)}")
