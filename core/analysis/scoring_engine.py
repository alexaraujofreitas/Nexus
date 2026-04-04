# ============================================================
# NEXUS TRADER — Trade Quality Scoring Engine (Phase 2)
#
# Deterministic 4-sub-score rubric:
#   overall = 0.35 * setup + 0.25 * risk + 0.20 * execution + 0.20 * decision
#
# Each sub-score starts at 100 and has penalties subtracted.
# Every penalty has an explicit evidence log entry with:
#   code, value, penalty_points, evidence_field, supporting_value
#
# Phase 2 additions:
#   • Regime confidence into setup and decision scoring
#   • HTF alignment penalty / bonus
#   • Entry extension / chase penalty
#   • Signal conflict severity
#   • Preventability and randomness composite hints
#   • Human-auditable trace with full evidence per penalty
#
# Classification thresholds:
#   GOOD    : overall >= 75 AND decision >= 70 AND setup >= 70 AND no hard overrides
#   BAD     : overall < 55 OR decision < 50 OR setup < 50 OR hard override triggered
#   NEUTRAL : everything else
# ============================================================
from __future__ import annotations

from typing import Optional
import logging

logger = logging.getLogger(__name__)

# ── Weights ───────────────────────────────────────────────────
WEIGHT_SETUP     = 0.35
WEIGHT_RISK      = 0.25
WEIGHT_EXECUTION = 0.20
WEIGHT_DECISION  = 0.20

# ── Thresholds ────────────────────────────────────────────────
MIN_CONFLUENCE   = 0.45
GOOD_CONFLUENCE  = 0.60
GREAT_CONFLUENCE = 0.75

MIN_RR           = 1.0
GOOD_RR          = 1.5
GREAT_RR         = 2.0

NOISE_DURATION_S = 300    # < 5 min = noise trade

MIN_REGIME_CONFIDENCE = 0.40   # below this = low-confidence regime
GOOD_REGIME_CONFIDENCE = 0.65  # above this = high-confidence regime

CHASE_PENALTY_THRESHOLD = 0.005  # > 0.5% above VWAP / fair value = chase

# ── Classification labels ─────────────────────────────────────
CLASSIFICATION_GOOD    = "GOOD"
CLASSIFICATION_BAD     = "BAD"
CLASSIFICATION_NEUTRAL = "NEUTRAL"

GOOD_OVERALL_THRESHOLD  = 75.0
GOOD_SETUP_THRESHOLD    = 70.0
GOOD_DECISION_THRESHOLD = 70.0
BAD_OVERALL_THRESHOLD   = 55.0
BAD_SETUP_THRESHOLD     = 50.0
BAD_DECISION_THRESHOLD  = 50.0

# ── Regime × direction affinity ───────────────────────────────
_AFFINITY: dict[tuple[str, str], int] = {
    ("bull_trend",             "buy"):  1,
    ("bull_trend",             "sell"): -1,
    ("bear_trend",             "buy"):  -1,
    ("bear_trend",             "sell"): 1,
    ("ranging",                "buy"):  0,
    ("ranging",                "sell"): 0,
    ("volatility_expansion",   "buy"):  0,
    ("volatility_expansion",   "sell"): 0,
    ("volatility_compression", "buy"):  0,
    ("volatility_compression", "sell"): 0,
    ("uncertain",              "buy"):  -1,
    ("uncertain",              "sell"): -1,
}


def _regime_affinity(regime: str, side: str) -> int:
    return _AFFINITY.get((regime.lower(), side.lower()), 0)


def _compute_rr(trade: dict) -> Optional[float]:
    try:
        entry = float(trade.get("entry_price") or 0)
        stop  = float(trade.get("stop_loss")   or 0)
        tp    = float(trade.get("take_profit") or 0)
        side  = (trade.get("side") or "buy").lower()
        if entry <= 0 or stop <= 0 or tp <= 0:
            return None
        if side == "buy":
            risk   = entry - stop
            reward = tp - entry
        else:
            risk   = stop - entry
            reward = entry - tp
        if risk <= 0:
            return None
        return reward / risk
    except Exception:
        return None


def _penalty_entry(
    code: str,
    points: float,
    evidence_field: str,
    supporting_value: object,
    note: str = "",
) -> str:
    """
    Build a human-auditable penalty log entry.
    Format: CODE:FIELD=VALUE:PENALTY:NOTE
    """
    note_part = f":{note}" if note else ""
    return f"{code}:{evidence_field}={supporting_value}:{int(points)}{note_part}"


# ─────────────────────────────────────────────────────────────
# Setup Quality  (0 – 100)
# ─────────────────────────────────────────────────────────────

def score_setup(trade: dict) -> tuple[float, list[str]]:
    """
    Evaluate pre-trade setup quality.
    Returns (score, penalty_log).
    Each log entry is a human-auditable string with field name and value.
    """
    score: float = 100.0
    log:   list[str] = []

    regime      = (trade.get("regime") or "uncertain").lower()
    side        = (trade.get("side")   or "buy").lower()
    confluence  = float(trade.get("score") or 0.0)
    models      = trade.get("models_fired") or []
    htf_conf    = trade.get("htf_confirmation")   # True / False / None
    regime_conf = float(trade.get("regime_confidence") or 0.0)
    entry_ext   = float(trade.get("entry_extension_pct") or 0.0)  # chase indicator

    # ── Regime alignment ──────────────────────────────────────
    affinity = _regime_affinity(regime, side)
    if affinity == -1:
        score -= 30
        log.append(_penalty_entry(
            f"COUNTER_REGIME:{side.upper()}_IN_{regime.upper()}",
            30, "regime", regime,
            f"side={side.upper()} opposes {regime.upper()}"
        ))
    elif regime == "uncertain":
        score -= 20
        log.append(_penalty_entry(
            "UNCERTAIN_REGIME", 20, "regime", regime,
            "classifier could not determine direction"
        ))
    elif affinity == 0:
        score -= 5
        log.append(_penalty_entry(
            f"NEUTRAL_REGIME:{regime.upper()}", 5, "regime", regime,
            "regime neutral — no directional support"
        ))

    # ── Regime confidence ─────────────────────────────────────
    if regime_conf > 0:
        if regime_conf < MIN_REGIME_CONFIDENCE:
            score -= 15
            log.append(_penalty_entry(
                "LOW_REGIME_CONFIDENCE", 15, "regime_confidence", f"{regime_conf:.2f}",
                f"classifier confidence {regime_conf:.0%} < {MIN_REGIME_CONFIDENCE:.0%}"
            ))
        elif regime_conf < GOOD_REGIME_CONFIDENCE and affinity == 0:
            score -= 5
            log.append(_penalty_entry(
                "MODERATE_REGIME_CONFIDENCE", 5, "regime_confidence", f"{regime_conf:.2f}",
                f"confidence {regime_conf:.0%} moderate for neutral regime"
            ))

    # ── HTF alignment ──────────────────────────────────────────
    if htf_conf is False:
        score -= 15
        log.append(_penalty_entry(
            "HTF_NOT_CONFIRMED", 15, "htf_confirmation", "False",
            "4h higher-timeframe direction NOT confirmed at entry"
        ))
    elif htf_conf is None and affinity == 1:
        # HTF unknown on a trend trade — mild uncertainty
        score -= 5
        log.append(_penalty_entry(
            "HTF_UNKNOWN", 5, "htf_confirmation", "None",
            "HTF status unknown for trend entry"
        ))
    # htf_conf is True → no penalty (bonus setup quality)

    # ── Confluence score ──────────────────────────────────────
    if confluence < MIN_CONFLUENCE:
        score -= 30
        log.append(_penalty_entry(
            f"BELOW_MIN_CONFLUENCE:{confluence:.3f}", 30,
            "score", f"{confluence:.3f}",
            f"confluence {confluence:.3f} below system gate {MIN_CONFLUENCE}"
        ))
    elif confluence < GOOD_CONFLUENCE:
        score -= 15
        log.append(_penalty_entry(
            f"LOW_CONFLUENCE:{confluence:.3f}", 15,
            "score", f"{confluence:.3f}",
            f"confluence {confluence:.3f} below preferred {GOOD_CONFLUENCE}"
        ))
    elif confluence < GREAT_CONFLUENCE:
        score -= 5
        log.append(_penalty_entry(
            f"MODERATE_CONFLUENCE:{confluence:.3f}", 5,
            "score", f"{confluence:.3f}",
            f"confluence {confluence:.3f} below great {GREAT_CONFLUENCE}"
        ))

    # ── Model agreement ───────────────────────────────────────
    if not models:
        score -= 25
        log.append(_penalty_entry(
            "NO_MODELS_FIRED", 25, "models_fired", "[]",
            "no signal models fired — no quantitative basis"
        ))
    elif len(models) == 1:
        score -= 10
        log.append(_penalty_entry(
            "SINGLE_MODEL_ONLY", 10, "models_fired", str(models),
            f"only 1 model ({models[0]}) fired — insufficient confirmation"
        ))

    # ── Entry extension / chase ───────────────────────────────
    if entry_ext > CHASE_PENALTY_THRESHOLD:
        pts = min(20, int(entry_ext * 2000))  # scale with extension size
        score -= pts
        log.append(_penalty_entry(
            f"ENTRY_CHASE:{entry_ext:.3f}", pts, "entry_extension_pct",
            f"{entry_ext:.3f}",
            f"entry extended {entry_ext:.2%} above fair value — chasing"
        ))

    return max(0.0, min(100.0, score)), log


# ─────────────────────────────────────────────────────────────
# Risk Management Quality  (0 – 100)
# ─────────────────────────────────────────────────────────────

def score_risk(trade: dict) -> tuple[float, list[str]]:
    score: float = 100.0
    log:   list[str] = []

    stop  = trade.get("stop_loss")
    tp    = trade.get("take_profit")
    entry = float(trade.get("entry_price") or 0.0)
    side  = (trade.get("side") or "buy").lower()
    atr   = float(trade.get("atr") or 0.0)

    # ── Stop loss present ─────────────────────────────────────
    if not stop or float(stop or 0) <= 0:
        score -= 50
        log.append(_penalty_entry(
            "NO_STOP_LOSS", 50, "stop_loss", "None",
            "hard override: unlimited risk — trade is always BAD"
        ))
        return max(0.0, score), log

    stop_f = float(stop)

    # ── Stop placement relative to ATR ────────────────────────
    if atr > 0 and entry > 0:
        if side == "buy":
            stop_dist = entry - stop_f
        else:
            stop_dist = stop_f - entry

        if stop_dist > 0:
            atr_mult = stop_dist / atr
            if atr_mult > 3.0:
                pts = min(20, int((atr_mult - 3.0) * 5))
                score -= pts
                log.append(_penalty_entry(
                    f"WIDE_STOP:{atr_mult:.1f}x_ATR", pts, "stop_loss",
                    f"{stop_f:.4g}",
                    f"stop is {atr_mult:.1f}× ATR ({atr:.4g}) — excessively wide"
                ))
            elif atr_mult < 0.5:
                score -= 10
                log.append(_penalty_entry(
                    f"TIGHT_STOP:{atr_mult:.2f}x_ATR", 10, "stop_loss",
                    f"{stop_f:.4g}",
                    f"stop is {atr_mult:.2f}× ATR ({atr:.4g}) — within market noise"
                ))

    # ── Take profit present ───────────────────────────────────
    if not tp or float(tp or 0) <= 0:
        score -= 20
        log.append(_penalty_entry(
            "NO_TAKE_PROFIT", 20, "take_profit", "None",
            "no defined exit target — R:R is undefined"
        ))

    # ── Risk : Reward ─────────────────────────────────────────
    rr = _compute_rr(trade)
    if rr is not None:
        if rr < MIN_RR:
            score -= 35
            log.append(_penalty_entry(
                f"RR_BELOW_FLOOR:{rr:.2f}", 35, "rr_ratio",
                f"{rr:.2f}",
                f"R:R {rr:.2f} < {MIN_RR} minimum — negative expectancy setup"
            ))
        elif rr < GOOD_RR:
            score -= 10
            log.append(_penalty_entry(
                f"LOW_RR:{rr:.2f}", 10, "rr_ratio",
                f"{rr:.2f}",
                f"R:R {rr:.2f} below preferred {GOOD_RR}"
            ))
        # >= GOOD_RR → no penalty
    elif tp and float(tp or 0) > 0:
        score -= 5
        log.append(_penalty_entry(
            "RR_UNCOMPUTABLE", 5, "rr_ratio", "None",
            "TP set but R:R could not be computed — price data issue"
        ))

    return max(0.0, min(100.0, score)), log


# ─────────────────────────────────────────────────────────────
# Execution Quality  (0 – 100)
# ─────────────────────────────────────────────────────────────

def score_execution(trade: dict) -> tuple[float, list[str]]:
    score: float = 100.0
    log:   list[str] = []

    exit_reason = (trade.get("exit_reason") or "").lower()
    pnl_pct     = float(trade.get("pnl_pct")    or 0.0)
    duration_s  = int(trade.get("duration_s")   or 0)
    entry       = float(trade.get("entry_price") or 0.0)
    exit_p      = float(trade.get("exit_price")  or 0.0)
    tp          = trade.get("take_profit")
    side        = (trade.get("side") or "buy").lower()
    slippage_pct= float(trade.get("slippage_pct") or 0.0)

    # ── Noise trade ───────────────────────────────────────────
    if 0 < duration_s < NOISE_DURATION_S:
        score -= 20
        log.append(_penalty_entry(
            f"NOISE_TRADE:{duration_s}s", 20, "duration_s",
            f"{duration_s}",
            f"trade lasted only {duration_s}s < {NOISE_DURATION_S}s minimum"
        ))

    # ── Slippage ──────────────────────────────────────────────
    if slippage_pct > 0.002:   # > 0.2% slippage
        pts = min(15, int(slippage_pct * 2000))
        score -= pts
        log.append(_penalty_entry(
            f"HIGH_SLIPPAGE:{slippage_pct:.3f}", pts, "slippage_pct",
            f"{slippage_pct:.3f}",
            f"entry slippage {slippage_pct:.2%} above 0.2% threshold"
        ))

    # ── Manual close evaluation ───────────────────────────────
    if exit_reason == "manual_close":
        if pnl_pct < 0:
            score -= 20
            log.append(_penalty_entry(
                "MANUAL_CLOSE_AT_LOSS", 20, "exit_reason",
                f"manual_close pnl_pct={pnl_pct:.2f}%",
                "manually closed at a loss — bypassed stop management"
            ))
        else:
            score -= 5
            log.append(_penalty_entry(
                "MANUAL_CLOSE", 5, "exit_reason", "manual_close",
                "manually closed — system exit management bypassed"
            ))

        # Premature exit: closed manually while TP not yet reached
        if tp and entry > 0 and exit_p > 0:
            tp_f = float(tp)
            if tp_f > 0:
                tp_proximity = 0.95
                if side == "buy" and exit_p < tp_f * tp_proximity:
                    # percentage progress toward TP
                    progress = (exit_p - entry) / (tp_f - entry) if tp_f > entry else 0
                    pts = 15 if progress < 0.5 else 10
                    score -= pts
                    log.append(_penalty_entry(
                        f"PREMATURE_EXIT:before_TP_zone:{progress:.0%}_progress",
                        pts, "exit_price",
                        f"{exit_p:.4g}",
                        f"closed at {progress:.0%} of TP range — not in TP zone"
                    ))
                elif side == "sell" and exit_p > tp_f / tp_proximity:
                    progress = (entry - exit_p) / (entry - tp_f) if tp_f < entry else 0
                    pts = 15 if progress < 0.5 else 10
                    score -= pts
                    log.append(_penalty_entry(
                        f"PREMATURE_EXIT:before_TP_zone:{progress:.0%}_progress",
                        pts, "exit_price",
                        f"{exit_p:.4g}",
                        f"closed at {progress:.0%} of TP range — not in TP zone"
                    ))

    return max(0.0, min(100.0, score)), log


# ─────────────────────────────────────────────────────────────
# Decision Quality  (0 – 100)
# ─────────────────────────────────────────────────────────────

def score_decision(trade: dict) -> tuple[float, list[str]]:
    score: float = 100.0
    log:   list[str] = []

    exit_reason    = (trade.get("exit_reason") or "").lower()
    pnl_pct        = float(trade.get("pnl_pct")    or 0.0)
    confluence     = float(trade.get("score")       or 0.0)
    regime         = (trade.get("regime")            or "uncertain").lower()
    side           = (trade.get("side")              or "buy").lower()
    models         = trade.get("models_fired")        or []
    htf_conf       = trade.get("htf_confirmation")
    regime_conf    = float(trade.get("regime_confidence") or 0.0)
    sig_conflict   = float(trade.get("signal_conflict_score") or 0.0)

    affinity = _regime_affinity(regime, side)

    # ── Confluence at decision time ───────────────────────────
    if confluence < MIN_CONFLUENCE:
        score -= 30
        log.append(_penalty_entry(
            f"TOOK_TRADE_BELOW_CONFLUENCE:{confluence:.3f}", 30,
            "score", f"{confluence:.3f}",
            f"entry taken with confluence {confluence:.3f} < minimum {MIN_CONFLUENCE}"
        ))
    elif confluence < GOOD_CONFLUENCE:
        score -= 10
        log.append(_penalty_entry(
            f"LOW_CONFLUENCE_DECISION:{confluence:.3f}", 10,
            "score", f"{confluence:.3f}",
            f"entry confluence {confluence:.3f} below preferred {GOOD_CONFLUENCE}"
        ))

    # ── Counter-regime decision ───────────────────────────────
    if affinity == -1:
        score -= 25
        log.append(_penalty_entry(
            f"COUNTER_REGIME_DECISION:{side.upper()}_IN_{regime.upper()}", 25,
            "regime", regime,
            f"deliberately entered {side.upper()} in {regime.upper()} regime"
        ))

    # ── Regime confidence at decision ─────────────────────────
    if regime_conf > 0 and regime_conf < MIN_REGIME_CONFIDENCE:
        score -= 10
        log.append(_penalty_entry(
            f"LOW_REGIME_CONFIDENCE_AT_DECISION:{regime_conf:.2f}", 10,
            "regime_confidence", f"{regime_conf:.2f}",
            f"entered with low regime confidence {regime_conf:.0%}"
        ))

    # ── HTF conflict ──────────────────────────────────────────
    if htf_conf is False and affinity == 1:
        # Trend trade without HTF confirmation is a decision flaw
        score -= 10
        log.append(_penalty_entry(
            "HTF_COUNTER_DECISION", 10, "htf_confirmation", "False",
            "entered trend trade without HTF 4h confirmation"
        ))

    # ── Signal conflict severity ──────────────────────────────
    if sig_conflict > 50:
        pts = min(20, int((sig_conflict - 50) * 0.4))
        score -= pts
        log.append(_penalty_entry(
            f"SIGNAL_CONFLICT_HIGH:{sig_conflict:.0f}%", pts,
            "signal_conflict_score", f"{sig_conflict:.0f}",
            f"signal conflict {sig_conflict:.0f}% — models disagreed significantly"
        ))

    # ── Manual close at a loss with acceptable setup ──────────
    if exit_reason == "manual_close" and pnl_pct < -1.0:
        score -= 15
        log.append(_penalty_entry(
            "MANUAL_LOSS_OVERRIDE:", 15, "exit_reason",
            f"manual_close pnl={pnl_pct:.2f}%",
            "manually closed at significant loss — bypassed system stop"
        ))

    # ── Held through reversal ─────────────────────────────────
    if exit_reason == "manual_close" and pnl_pct < -2.0 and confluence >= GOOD_CONFLUENCE:
        score -= 10
        log.append(_penalty_entry(
            "HELD_THROUGH_REVERSAL:", 10, "pnl_pct",
            f"{pnl_pct:.2f}%",
            f"high-confluence trade ({confluence:.2f}) closed at large loss — possible reversal"
        ))

    # ── Stop hit on counter-regime entry ─────────────────────
    if exit_reason == "stop_loss" and affinity == -1:
        score -= 10
        log.append(_penalty_entry(
            "STOP_HIT_COUNTER_REGIME_ENTRY:", 10,
            "regime", regime,
            "stop hit on counter-regime trade — regime was the root cause"
        ))

    # ── No models ─────────────────────────────────────────────
    if not models:
        score -= 15
        log.append(_penalty_entry(
            "DECISION_WITHOUT_MODELS:", 15, "models_fired", "[]",
            "no quantitative model basis for this decision"
        ))

    return max(0.0, min(100.0, score)), log


# ─────────────────────────────────────────────────────────────
# Hard overrides
# ─────────────────────────────────────────────────────────────

def compute_hard_overrides(trade: dict) -> list[str]:
    overrides: list[str] = []

    stop = trade.get("stop_loss")
    if not stop or float(stop or 0) <= 0:
        overrides.append("HARD_OVERRIDE:NO_STOP_LOSS")

    confluence = float(trade.get("score") or 0.0)
    if confluence < MIN_CONFLUENCE:
        overrides.append(f"HARD_OVERRIDE:BELOW_MIN_CONFLUENCE:{confluence:.3f}")

    rr = _compute_rr(trade)
    if rr is not None and rr < MIN_RR:
        overrides.append(f"HARD_OVERRIDE:RR_BELOW_FLOOR:{rr:.2f}")

    return overrides


# ─────────────────────────────────────────────────────────────
# Master scoring function
# ─────────────────────────────────────────────────────────────

def score_trade(trade: dict) -> dict:
    """
    Master scoring function. Returns the complete scoring result dict.

    All penalty_log entries now follow the format:
        CODE:FIELD=VALUE:PENALTY_POINTS:NOTE
    This makes the trace human-auditable: operators can see exactly
    which field triggered which penalty at what magnitude and why.

    Returns
    -------
    {
        setup_score, risk_score, execution_score, decision_score,
        overall_score, classification, hard_overrides,
        penalty_log   : {setup: [...], risk: [...], execution: [...], decision: [...]},
        rr_ratio, regime_affinity, is_open,
        regime_confidence_at_entry,
        htf_confirmed_at_entry,
    }
    """
    setup_s,  setup_log  = score_setup(trade)
    risk_s,   risk_log   = score_risk(trade)
    exec_s,   exec_log   = score_execution(trade)
    dec_s,    dec_log    = score_decision(trade)

    is_open = not bool(trade.get("exit_price") or trade.get("exit_reason"))
    if is_open:
        overall = (
            WEIGHT_SETUP * setup_s
            + WEIGHT_RISK * risk_s
            + (WEIGHT_EXECUTION + WEIGHT_DECISION) * 50.0  # neutral placeholder
        )
    else:
        overall = (
            WEIGHT_SETUP     * setup_s
            + WEIGHT_RISK      * risk_s
            + WEIGHT_EXECUTION * exec_s
            + WEIGHT_DECISION  * dec_s
        )

    overall = round(max(0.0, min(100.0, overall)), 1)

    hard_overrides = compute_hard_overrides(trade)
    regime = (trade.get("regime") or "uncertain").lower()
    side   = (trade.get("side")   or "buy").lower()

    # ── Classify ─────────────────────────────────────────────
    if hard_overrides:
        classification = CLASSIFICATION_BAD
    elif (
        overall      >= GOOD_OVERALL_THRESHOLD
        and setup_s  >= GOOD_SETUP_THRESHOLD
        and dec_s    >= GOOD_DECISION_THRESHOLD
    ):
        classification = CLASSIFICATION_GOOD
    elif (
        overall < BAD_OVERALL_THRESHOLD
        or setup_s  < BAD_SETUP_THRESHOLD
        or dec_s    < BAD_DECISION_THRESHOLD
    ):
        classification = CLASSIFICATION_BAD
    else:
        classification = CLASSIFICATION_NEUTRAL

    return {
        "setup_score":      round(setup_s,  1),
        "risk_score":       round(risk_s,   1),
        "execution_score":  round(exec_s,   1),
        "decision_score":   round(dec_s,    1),
        "overall_score":    overall,
        "classification":   classification,
        "hard_overrides":   hard_overrides,
        "penalty_log": {
            "setup":     setup_log,
            "risk":      risk_log,
            "execution": exec_log,
            "decision":  dec_log,
        },
        "rr_ratio":                  _compute_rr(trade),
        "regime_affinity":           _regime_affinity(regime, side),
        "is_open":                   is_open,
        "regime_confidence_at_entry":float(trade.get("regime_confidence") or 0.0),
        "htf_confirmed_at_entry":    trade.get("htf_confirmation"),
    }
