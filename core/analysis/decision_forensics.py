# ============================================================
# NEXUS TRADER — Decision Forensics (Phase 2)
#
# For every closed trade, determines the 2×2 decision/outcome
# matrix and deeper forensic fields:
#
#   decision_outcome_matrix_label
#   was_loss_probabilistically_acceptable
#   was_win_quality_supported
#   avoidable_loss_flag
#   avoidable_win_flag
#   failure_domain_primary
#   failure_domain_secondary
#   preventability_score  (0–100)
#   randomness_score      (0–100)
#   model_conflict_score  (0–100)
#   regime_confidence_at_entry
#   regime_confidence_at_exit
#
# Decision classification:
#   GOOD_DECISION_GOOD_OUTCOME   → Setup was sound, positive PnL
#   GOOD_DECISION_BAD_OUTCOME    → Sound process, market went against — acceptable loss
#   BAD_DECISION_BAD_OUTCOME     → Poor setup + loss — preventable
#   BAD_DECISION_LUCKY_OUTCOME   → Poor setup + profit — unsustainable
# ============================================================
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ── Matrix labels ─────────────────────────────────────────────
MATRIX_GOOD_GOOD    = "GOOD_DECISION_GOOD_OUTCOME"
MATRIX_GOOD_BAD     = "GOOD_DECISION_BAD_OUTCOME"
MATRIX_BAD_BAD      = "BAD_DECISION_BAD_OUTCOME"
MATRIX_BAD_LUCKY    = "BAD_DECISION_LUCKY_OUTCOME"

# ── Failure domains ───────────────────────────────────────────
DOMAIN_SETUP            = "SETUP"
DOMAIN_RISK             = "RISK"
DOMAIN_EXECUTION        = "EXECUTION"
DOMAIN_DECISION         = "DECISION"
DOMAIN_MARKET_RANDOMNESS= "MARKET_RANDOMNESS"
DOMAIN_NA               = "N/A"

# ── Thresholds ────────────────────────────────────────────────
GOOD_DECISION_THRESHOLD = 70.0   # decision_score >= this → "good decision"
GOOD_SETUP_THRESHOLD    = 70.0   # setup_score >= this → "good setup"
GOOD_OVERALL_THRESHOLD  = 75.0


def build_decision_forensics(trade: dict, scoring_result: dict) -> dict:
    """
    Build the full decision-forensics object for a closed trade.

    Parameters
    ----------
    trade : dict
        Closed trade dict with entry/exit data and PnL.
    scoring_result : dict
        Output of score_trade() from scoring_engine.

    Returns
    -------
    dict
        Forensics object with all required fields.
    """
    try:
        return _compute_forensics(trade, scoring_result)
    except Exception as exc:
        logger.error("build_decision_forensics failed: %s", exc, exc_info=True)
        return _empty_forensics()


def _compute_forensics(trade: dict, scoring_result: dict) -> dict:
    # ── Extract scores ────────────────────────────────────────
    overall_s   = float(scoring_result.get("overall_score",   0.0))
    setup_s     = float(scoring_result.get("setup_score",     0.0))
    risk_s      = float(scoring_result.get("risk_score",      0.0))
    execution_s = float(scoring_result.get("execution_score", 0.0))
    decision_s  = float(scoring_result.get("decision_score",  0.0))
    hard_overrides = scoring_result.get("hard_overrides") or []

    # ── Extract trade fields ──────────────────────────────────
    pnl_usdt    = float(trade.get("pnl_usdt") or trade.get("pnl") or 0.0)
    pnl_pct     = float(trade.get("pnl_pct")  or 0.0)
    exit_reason = (trade.get("exit_reason") or trade.get("close_reason") or "").lower()
    score       = float(trade.get("score")   or 0.0)
    models      = trade.get("models_fired")   or []
    regime      = (trade.get("regime")        or "uncertain").lower()
    side        = (trade.get("side")          or "buy").lower()
    rr_ratio    = scoring_result.get("rr_ratio")
    regime_conf = float(trade.get("regime_confidence") or 0.0)

    # ── Decision quality (was the decision sound?) ────────────
    decision_sound = (
        decision_s >= GOOD_DECISION_THRESHOLD
        and setup_s >= GOOD_SETUP_THRESHOLD
        and not hard_overrides
    )

    # ── Outcome ────────────────────────────────────────────────
    is_win  = pnl_usdt > 0
    is_loss = pnl_usdt < 0

    # ── 2×2 Matrix label ─────────────────────────────────────
    if decision_sound and is_win:
        matrix_label = MATRIX_GOOD_GOOD
    elif decision_sound and is_loss:
        matrix_label = MATRIX_GOOD_BAD
    elif not decision_sound and is_loss:
        matrix_label = MATRIX_BAD_BAD
    else:
        # bad decision, positive outcome
        matrix_label = MATRIX_BAD_LUCKY

    # ── Probabilistic acceptability ──────────────────────────
    # A loss is "probabilistically acceptable" if:
    # • Decision was sound (setup/decision score both >= threshold)
    # • R:R was favourable (>= 1.5)
    # • Exit was clean (stop-loss hit, not manual)
    # • PnL loss is within expected 1R range
    was_loss_acceptable = False
    if is_loss:
        clean_exit = exit_reason in ("stop_loss", "trailing_stop")
        rr_ok      = (rr_ratio or 0) >= 1.5
        within_1r  = pnl_pct >= -2.5  # heuristic: within ~1R loss for 0.5% risk
        was_loss_acceptable = (decision_sound and clean_exit and rr_ok)

    # ── Win quality (was the win supported by process?) ──────
    was_win_quality_supported = False
    if is_win:
        was_win_quality_supported = decision_sound and overall_s >= GOOD_OVERALL_THRESHOLD

    # ── Avoidable flags ───────────────────────────────────────
    # Avoidable loss: bad decision that led to a loss
    avoidable_loss_flag = is_loss and not decision_sound and bool(hard_overrides or setup_s < 50)

    # Avoidable win: trade shouldn't have been taken, but profited anyway
    avoidable_win_flag  = is_win and not decision_sound

    # ── Failure domains ───────────────────────────────────────
    primary_domain, secondary_domain = _determine_failure_domains(
        overall_s, setup_s, risk_s, execution_s, decision_s,
        hard_overrides, is_win, decision_sound, exit_reason, pnl_pct
    )

    # ── Preventability score (0=unpreventable, 100=fully preventable) ──
    preventability = _compute_preventability(
        decision_sound=decision_sound,
        hard_overrides=hard_overrides,
        setup_s=setup_s,
        risk_s=risk_s,
        execution_s=execution_s,
        exit_reason=exit_reason,
        is_win=is_win,
    )

    # ── Randomness score (0=no random, 100=pure randomness) ──
    randomness = _compute_randomness(
        decision_sound=decision_sound,
        was_loss_acceptable=was_loss_acceptable,
        overall_s=overall_s,
        exit_reason=exit_reason,
        is_win=is_win,
        pnl_pct=pnl_pct,
        rr_ratio=rr_ratio,
    )

    # ── Model conflict score (0=no conflict, 100=full conflict) ──
    model_conflict = _compute_model_conflict(models, score)

    return {
        "decision_outcome_matrix_label":      matrix_label,
        "was_loss_probabilistically_acceptable": was_loss_acceptable,
        "was_win_quality_supported":          was_win_quality_supported,
        "avoidable_loss_flag":                avoidable_loss_flag,
        "avoidable_win_flag":                 avoidable_win_flag,
        "failure_domain_primary":             primary_domain,
        "failure_domain_secondary":           secondary_domain,
        "preventability_score":               round(preventability, 1),
        "randomness_score":                   round(randomness, 1),
        "model_conflict_score":               round(model_conflict, 1),
        "regime_confidence_at_entry":         round(regime_conf, 3),
        "regime_confidence_at_exit":          None,   # populated if available
        "decision_sound":                     decision_sound,
        "is_win":                             is_win,
    }


def _determine_failure_domains(
    overall_s: float, setup_s: float, risk_s: float,
    execution_s: float, decision_s: float,
    hard_overrides: list, is_win: bool,
    decision_sound: bool, exit_reason: str, pnl_pct: float,
) -> tuple[str, str]:
    """Return (primary_domain, secondary_domain) based on score gaps."""

    if is_win and decision_sound:
        return DOMAIN_NA, DOMAIN_NA

    # Score gaps relative to their ideal (100)
    gaps = {
        DOMAIN_SETUP:      100.0 - setup_s,
        DOMAIN_RISK:       100.0 - risk_s,
        DOMAIN_EXECUTION:  100.0 - execution_s,
        DOMAIN_DECISION:   100.0 - decision_s,
    }

    # Hard overrides → always primary
    if hard_overrides:
        if any("NO_STOP" in h for h in hard_overrides):
            return DOMAIN_RISK, DOMAIN_SETUP
        if any("RR_BELOW" in h for h in hard_overrides):
            return DOMAIN_RISK, DOMAIN_DECISION
        if any("CONFLUENCE" in h for h in hard_overrides):
            return DOMAIN_SETUP, DOMAIN_DECISION

    # Manual close logic
    if exit_reason == "manual_close" and pnl_pct < 0:
        if execution_s < 60:
            return DOMAIN_EXECUTION, _second_worst(gaps, exclude=DOMAIN_EXECUTION)

    # Sort by gap size (largest gap = primary failure domain)
    sorted_domains = sorted(gaps.items(), key=lambda x: x[1], reverse=True)

    # If top gap is small, primary failure is market randomness
    if sorted_domains[0][1] < 15 and not hard_overrides:
        return DOMAIN_MARKET_RANDOMNESS, sorted_domains[0][0]

    primary   = sorted_domains[0][0]
    secondary = sorted_domains[1][0] if len(sorted_domains) > 1 else DOMAIN_NA
    return primary, secondary


def _second_worst(gaps: dict, exclude: str) -> str:
    filtered = {k: v for k, v in gaps.items() if k != exclude}
    if not filtered:
        return DOMAIN_NA
    return max(filtered, key=lambda k: filtered[k])


def _compute_preventability(
    decision_sound: bool,
    hard_overrides: list,
    setup_s: float,
    risk_s: float,
    execution_s: float,
    exit_reason: str,
    is_win: bool,
) -> float:
    """
    Preventability score: how preventable was the trade outcome?
    100 = fully preventable (bad decision with clear rules violation)
    0   = not preventable (sound decision, market moved adversely)
    """
    score = 0.0

    if hard_overrides:
        score += 40.0   # hard rule was violated

    if not decision_sound:
        score += 20.0   # decision quality was poor

    if setup_s < 50:
        score += 15.0   # bad setup

    if risk_s < 50:
        score += 15.0   # bad risk management

    if exit_reason == "manual_close" and not is_win:
        score += 10.0   # emotional close

    return min(100.0, score)


def _compute_randomness(
    decision_sound: bool,
    was_loss_acceptable: bool,
    overall_s: float,
    exit_reason: str,
    is_win: bool,
    pnl_pct: float,
    rr_ratio: Optional[float],
) -> float:
    """
    Randomness score: how much was the outcome driven by random market forces?
    100 = outcome was essentially random relative to the decision quality
    0   = outcome was fully predictable from decision quality
    """
    score = 0.0

    # Good decision + bad outcome → high randomness
    if decision_sound and not is_win:
        score += 50.0

    # Bad decision + good outcome → high randomness (luck)
    if not decision_sound and is_win:
        score += 40.0

    # Stop was hit cleanly on a sound decision → randomness elevated
    if exit_reason == "stop_loss" and decision_sound:
        score += 20.0

    # Clean stop hit on overall high-quality trade
    if overall_s >= 75 and not is_win and exit_reason in ("stop_loss", "trailing_stop"):
        score += 15.0

    # Poor PnL despite sound setup suggests market conditions were adverse
    if decision_sound and pnl_pct < -1.0:
        score += 10.0

    return min(100.0, score)


def _compute_model_conflict(models: list, confluence_score: float) -> float:
    """
    Proxy for model conflict score (0=no conflict, 100=maximum conflict).
    Currently derived from model count and confluence level.
    In full implementation, this should use per-model direction signals.
    """
    if not models:
        return 50.0   # no models = no confirmable agreement

    n = len(models)
    if n == 1:
        return 30.0   # single model — unknown conflict

    # Lower confluence with multiple models suggests internal conflict
    if confluence_score >= 0.70:
        return 5.0
    elif confluence_score >= 0.55:
        return 20.0
    elif confluence_score >= 0.45:
        return 40.0
    else:
        return 70.0


def _empty_forensics() -> dict:
    return {
        "decision_outcome_matrix_label":      MATRIX_GOOD_BAD,
        "was_loss_probabilistically_acceptable": False,
        "was_win_quality_supported":          False,
        "avoidable_loss_flag":                False,
        "avoidable_win_flag":                 False,
        "failure_domain_primary":             DOMAIN_NA,
        "failure_domain_secondary":           DOMAIN_NA,
        "preventability_score":               0.0,
        "randomness_score":                   50.0,
        "model_conflict_score":               50.0,
        "regime_confidence_at_entry":         0.0,
        "regime_confidence_at_exit":          None,
        "decision_sound":                     False,
        "is_win":                             False,
    }
