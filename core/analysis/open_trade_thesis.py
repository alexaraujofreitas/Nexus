# ============================================================
# NEXUS TRADER — Open Trade Thesis Generator (Phase 2)
#
# For every open position, constructs a full trade thesis object
# answering:
#   1. Why was this trade opened? (entry evidence)
#   2. What makes it a valid candidate? (signal breakdown)
#   3. Is the original thesis still valid? (live validity)
#   4. What should NexusTrader do now? (disposition)
#
# Required output fields:
#   entry_thesis_summary            str
#   entry_signal_evidence           list[dict]
#   entry_regime_alignment_summary  str
#   entry_htf_alignment_summary     str
#   entry_risk_reward_summary       str
#   entry_model_contribution_breakdown  list[dict]
#   live_validity_summary           str
#   live_validity_score             float  0–100
#   current_disposition             str
#   current_disposition_reason      str
#   thesis_changed_since_entry      bool
#   thesis_change_factors           list[str]
#   thesis_status                   str  (intact|weakening|invalidated|improved)
#   thesis_status_reason            str
#   regime_shift_detected           bool
#   confluence_change_since_entry   float  None if unknown
#   momentum_change_since_entry     str    None if unknown
#   volatility_change_since_entry   str    None if unknown
#   liquidity_change_since_entry    str    None if unknown
# ============================================================
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ── Disposition constants ─────────────────────────────────────
DISP_HOLD          = "Hold"
DISP_HOLD_REDUCE   = "Hold (Reduce Risk)"
DISP_TIGHTEN_STOP  = "Tighten Stop"
DISP_PARTIAL_TP    = "Partial Take Profit"
DISP_EXIT_EARLY    = "Exit Early"

# ── Thesis status constants ───────────────────────────────────
THESIS_INTACT       = "intact"
THESIS_WEAKENING    = "weakening"
THESIS_INVALIDATED  = "invalidated"
THESIS_IMPROVED     = "improved"

# ── Regime affinity (mirrors scoring_engine) ─────────────────
_AFFINITY: dict[tuple[str, str], int] = {
    ("bull_trend",            "buy"):  1,
    ("bull_trend",            "sell"): -1,
    ("bear_trend",            "buy"):  -1,
    ("bear_trend",            "sell"): 1,
    ("ranging",               "buy"):  0,
    ("ranging",               "sell"): 0,
    ("volatility_expansion",  "buy"):  0,
    ("volatility_expansion",  "sell"): 0,
    ("volatility_compression","buy"):  0,
    ("volatility_compression","sell"): 0,
    ("uncertain",             "buy"):  -1,
    ("uncertain",             "sell"): -1,
}

_REGIME_DISPLAY = {
    "bull_trend": "Bull Trend",
    "bear_trend": "Bear Trend",
    "ranging": "Ranging",
    "volatility_expansion": "Volatility Expansion",
    "volatility_compression": "Volatility Compression",
    "uncertain": "Uncertain",
}

_MODEL_DISPLAY = {
    "trend":              "Trend Model",
    "momentum_breakout":  "Momentum Breakout",
    "vwap_reversion":     "VWAP Reversion",
    "order_book":         "Order Book",
    "funding_rate":       "Funding Rate",
    "mean_reversion":     "Mean Reversion",
    "liquidity_sweep":    "Liquidity Sweep",
}


def build_open_trade_thesis(
    trade: dict,
    current_regime: Optional[str] = None,
    current_confluence: Optional[float] = None,
    current_momentum: Optional[str] = None,
    current_volatility: Optional[str] = None,
    current_liquidity: Optional[str] = None,
) -> dict:
    """
    Build a full open-trade thesis object from stored entry data plus optional
    current-market snapshot fields for live validity assessment.

    Parameters
    ----------
    trade : dict
        Open position dict (from paper_executor / positions dict).
    current_regime : str | None
        Live regime string, if available.
    current_confluence : float | None
        Current confluence level for this symbol (0–1), if available.
    current_momentum : str | None
        "increasing" | "decreasing" | "neutral", if available.
    current_volatility : str | None
        "expanding" | "contracting" | "normal", if available.
    current_liquidity : str | None
        "improving" | "declining" | "normal", if available.

    Returns
    -------
    dict
        Full thesis object with all required fields.
    """
    try:
        return _build_thesis(
            trade, current_regime, current_confluence,
            current_momentum, current_volatility, current_liquidity
        )
    except Exception as exc:
        logger.error("build_open_trade_thesis failed: %s", exc, exc_info=True)
        return _empty_thesis()


def _build_thesis(
    trade: dict,
    current_regime: Optional[str],
    current_confluence: Optional[float],
    current_momentum: Optional[str],
    current_volatility: Optional[str],
    current_liquidity: Optional[str],
) -> dict:
    # ── Entry context ─────────────────────────────────────────
    symbol     = trade.get("symbol") or "?"
    side       = (trade.get("side") or "buy").lower()
    regime     = (trade.get("regime") or "uncertain").lower()
    score      = float(trade.get("score") or 0.0)
    models     = trade.get("models_fired") or []
    entry_px   = float(trade.get("entry_price") or 0.0)
    stop_loss  = trade.get("stop_loss")
    take_profit= trade.get("take_profit")
    htf_conf   = trade.get("htf_confirmation")  # True/False/None
    regime_conf= float(trade.get("regime_confidence") or 0.0)
    opened_at  = trade.get("opened_at") or ""

    # ── Derived ───────────────────────────────────────────────
    affinity     = _AFFINITY.get((regime, side), 0)
    regime_label = _REGIME_DISPLAY.get(regime, regime.replace("_", " ").title())
    side_label   = side.upper()

    # ── 1. Entry thesis summary ───────────────────────────────
    thesis_summary = _build_entry_summary(
        symbol, side_label, regime_label, affinity, score, models, htf_conf, entry_px
    )

    # ── 2. Signal evidence items ──────────────────────────────
    signal_evidence = _build_signal_evidence(
        score, regime, side, models, htf_conf, regime_conf, stop_loss, take_profit, entry_px
    )

    # ── 3. Regime alignment summary ──────────────────────────
    regime_summary = _build_regime_summary(regime_label, affinity, regime_conf, side_label)

    # ── 4. HTF alignment summary ──────────────────────────────
    htf_summary = _build_htf_summary(htf_conf)

    # ── 5. Risk/Reward summary ────────────────────────────────
    rr_summary = _build_rr_summary(entry_px, stop_loss, take_profit, side)

    # ── 6. Model contribution breakdown ──────────────────────
    model_breakdown = _build_model_breakdown(models, score)

    # ── 7. Live validity assessment ───────────────────────────
    (
        live_score, live_summary, thesis_status, thesis_status_reason,
        thesis_changed, change_factors,
        regime_shift, conf_change, disp, disp_reason
    ) = _compute_live_validity(
        regime=regime, side=side, score=score, models=models,
        htf_conf=htf_conf, stop_loss=stop_loss, take_profit=take_profit,
        entry_px=entry_px,
        current_regime=current_regime,
        current_confluence=current_confluence,
        current_momentum=current_momentum,
        current_volatility=current_volatility,
    )

    return {
        # Entry
        "entry_thesis_summary":             thesis_summary,
        "entry_signal_evidence":            signal_evidence,
        "entry_regime_alignment_summary":   regime_summary,
        "entry_htf_alignment_summary":      htf_summary,
        "entry_risk_reward_summary":        rr_summary,
        "entry_model_contribution_breakdown": model_breakdown,
        # Live validity
        "live_validity_summary":            live_summary,
        "live_validity_score":              round(live_score, 1),
        "current_disposition":             disp,
        "current_disposition_reason":      disp_reason,
        "thesis_changed_since_entry":       thesis_changed,
        "thesis_change_factors":            change_factors,
        "thesis_status":                    thesis_status,
        "thesis_status_reason":             thesis_status_reason,
        # Change metrics
        "regime_shift_detected":            regime_shift,
        "confluence_change_since_entry":    conf_change,
        "momentum_change_since_entry":      current_momentum,
        "volatility_change_since_entry":    current_volatility,
        "liquidity_change_since_entry":     current_liquidity,
    }


# ── Entry evidence builders ───────────────────────────────────

def _build_entry_summary(
    symbol: str, side: str, regime_label: str,
    affinity: int, score: float, models: list,
    htf_conf, entry_px: float
) -> str:
    model_count = len(models) if models else 0
    model_str   = f"{model_count} model{'s' if model_count != 1 else ''}"

    if affinity == 1:
        regime_clause = f"{regime_label} regime fully supports this direction"
    elif affinity == 0:
        regime_clause = f"{regime_label} regime is neutral to this direction"
    else:
        regime_clause = f"{regime_label} regime is counter to this direction"

    htf_clause = (
        " HTF 4h confirmed entry."   if htf_conf is True  else
        " HTF 4h NOT confirmed."     if htf_conf is False else
        ""
    )

    return (
        f"{symbol} {side} entered at {entry_px:.4g}. "
        f"{regime_clause}, confluence {score:.2f}, {model_str} fired.{htf_clause}"
    )


def _build_signal_evidence(
    score: float, regime: str, side: str, models: list,
    htf_conf, regime_conf: float, stop_loss, take_profit,
    entry_px: float
) -> list[dict]:
    evidence = []

    # Confluence
    if score >= 0.75:
        conf_quality = "strong"
    elif score >= 0.60:
        conf_quality = "good"
    elif score >= 0.45:
        conf_quality = "marginal"
    else:
        conf_quality = "below minimum"
    evidence.append({
        "type": "confluence",
        "label": f"Confluence: {score:.2f}",
        "detail": f"Signal agreement is {conf_quality} ({score:.2f})",
        "positive": score >= 0.60,
    })

    # Regime
    affinity = _AFFINITY.get((regime, side), 0)
    evidence.append({
        "type": "regime",
        "label": f"Regime: {_REGIME_DISPLAY.get(regime, regime)} (affinity={affinity:+d})",
        "detail": (
            "Regime fully supports direction" if affinity == 1 else
            "Regime neutral to direction" if affinity == 0 else
            "Regime counter to direction — elevated risk"
        ),
        "positive": affinity >= 0,
    })

    # Regime confidence
    if regime_conf > 0:
        evidence.append({
            "type": "regime_confidence",
            "label": f"Regime confidence: {regime_conf:.0%}",
            "detail": (
                f"Regime classifier confidence was {regime_conf:.0%} at entry"
            ),
            "positive": regime_conf >= 0.60,
        })

    # HTF alignment
    if htf_conf is not None:
        evidence.append({
            "type": "htf_alignment",
            "label": "HTF 4h: Confirmed" if htf_conf else "HTF 4h: NOT confirmed",
            "detail": (
                "Higher-timeframe 4h confirmed entry direction"
                if htf_conf else
                "Higher-timeframe 4h did NOT confirm entry direction"
            ),
            "positive": bool(htf_conf),
        })

    # Models
    if models:
        for m in models:
            evidence.append({
                "type": "model",
                "label": _MODEL_DISPLAY.get(m, m),
                "detail": f"Model '{_MODEL_DISPLAY.get(m, m)}' fired at entry",
                "positive": True,
            })
    else:
        evidence.append({
            "type": "model",
            "label": "No models fired",
            "detail": "No signal model triggered this entry — no quantitative basis",
            "positive": False,
        })

    # Stop/TP
    if stop_loss and float(stop_loss or 0) > 0:
        evidence.append({
            "type": "risk",
            "label": f"Stop-loss set: {float(stop_loss):.4g}",
            "detail": "Stop-loss is defined — risk is bounded",
            "positive": True,
        })
    else:
        evidence.append({
            "type": "risk",
            "label": "No stop-loss set",
            "detail": "Stop-loss is NOT set — unlimited downside risk",
            "positive": False,
        })

    return evidence


def _build_regime_summary(
    regime_label: str, affinity: int, regime_conf: float, side: str
) -> str:
    if affinity == 1:
        align_str = f"fully aligned with {side} direction"
    elif affinity == 0:
        align_str = f"neutral — no directional support or opposition for {side}"
    else:
        align_str = f"counter to {side} direction — increased adverse probability"

    conf_str = (
        f" Regime confidence at entry: {regime_conf:.0%}." if regime_conf > 0 else ""
    )
    return f"{regime_label} regime is {align_str}.{conf_str}"


def _build_htf_summary(htf_conf) -> str:
    if htf_conf is True:
        return "4h higher-timeframe analysis confirmed the trade direction at entry."
    elif htf_conf is False:
        return (
            "4h higher-timeframe did NOT confirm entry direction. "
            "This increases the risk of a counter-trend trade at the HTF level."
        )
    return "Higher-timeframe confirmation status was not recorded at entry."


def _build_rr_summary(entry_px: float, stop_loss, take_profit, side: str) -> str:
    sl = float(stop_loss   or 0)
    tp = float(take_profit or 0)

    if entry_px <= 0 or sl <= 0:
        return "R:R cannot be computed — stop-loss not set."

    if side == "buy":
        risk   = entry_px - sl
        reward = tp - entry_px if tp > 0 else None
    else:
        risk   = sl - entry_px
        reward = entry_px - tp if tp > 0 else None

    if risk <= 0:
        return "R:R cannot be computed — stop-loss placement error."

    risk_pct = abs(risk / entry_px) * 100

    if reward is None or reward <= 0:
        return (
            f"No take-profit set. Risk: {risk_pct:.2f}% per trade. "
            "Without a defined target, R:R is undefined."
        )

    rr = reward / risk
    qual = (
        "excellent" if rr >= 2.0 else
        "good"      if rr >= 1.5 else
        "acceptable" if rr >= 1.0 else
        "below floor — negative expectancy"
    )
    return (
        f"Entry {entry_px:.4g} | Stop {sl:.4g} | Target {tp:.4g}. "
        f"Theoretical R:R {rr:.2f}:1 ({qual}). Risk: {risk_pct:.2f}% of entry."
    )


def _build_model_breakdown(models: list, total_score: float) -> list[dict]:
    if not models:
        return [{
            "model": "none",
            "display": "No models",
            "contribution_note": "No signal model fired — trade has no quantitative basis",
            "weight_pct": 0,
        }]

    # Approximate equal weight per model as a contribution indicator
    n = len(models)
    per_model_approx = round(total_score / n, 2) if n > 0 else 0.0

    result = []
    for m in models:
        result.append({
            "model":             m,
            "display":           _MODEL_DISPLAY.get(m, m),
            "contribution_note": f"Approx. {per_model_approx:.2f} confluence contribution (equal-weight estimate)",
            "weight_pct":        round(100 / n, 1),
        })
    return result


# ── Live validity assessment ──────────────────────────────────

def _compute_live_validity(
    regime: str, side: str, score: float, models: list,
    htf_conf, stop_loss, take_profit, entry_px: float,
    current_regime: Optional[str],
    current_confluence: Optional[float],
    current_momentum: Optional[str],
    current_volatility: Optional[str],
) -> tuple:
    """
    Returns:
        (live_score, live_summary, thesis_status, status_reason,
         thesis_changed, change_factors,
         regime_shift_detected, conf_change_since_entry,
         disposition, disposition_reason)
    """
    live_score = 100.0
    change_factors: list[str] = []
    thesis_changed = False
    regime_shift = False
    conf_change: Optional[float] = None

    # ── Baseline quality adjustment ────────────────────────────
    affinity = _AFFINITY.get((regime, side), 0)
    if affinity == -1:
        live_score -= 30
        change_factors.append("Entry was already counter-regime at open")
    elif affinity == 0:
        live_score -= 10

    if score < 0.45:
        live_score -= 25
        change_factors.append("Entry confluence was below minimum threshold")
    elif score < 0.60:
        live_score -= 10

    if not models:
        live_score -= 20
        change_factors.append("No signal models fired at entry")
    elif len(models) == 1:
        live_score -= 5

    stop_set = stop_loss and float(stop_loss or 0) > 0
    if not stop_set:
        live_score -= 20
        change_factors.append("No stop-loss set — unlimited risk")

    # ── Current market state adjustments ──────────────────────
    if current_regime is not None:
        cur_reg = current_regime.lower()
        if cur_reg != regime:
            regime_shift = True
            thesis_changed = True
            cur_aff = _AFFINITY.get((cur_reg, side), 0)
            if cur_aff == -1:
                live_score -= 25
                change_factors.append(
                    f"Regime shifted from {regime} → {cur_reg} (now counter-regime)"
                )
            elif cur_aff == 0 and affinity == 1:
                live_score -= 10
                change_factors.append(
                    f"Regime weakened from {regime} → {cur_reg} (no longer supporting)"
                )
            elif cur_aff == 1 and affinity != 1:
                live_score += 10  # regime improved
                change_factors.append(
                    f"Regime improved from {regime} → {cur_reg} (now supporting)"
                )

    if current_confluence is not None:
        conf_change = current_confluence - score
        if conf_change < -0.10:
            live_score -= 15
            change_factors.append(
                f"Confluence dropped {abs(conf_change):.2f} since entry (now {current_confluence:.2f})"
            )
            thesis_changed = True
        elif conf_change > 0.10:
            live_score += 10
            change_factors.append(
                f"Confluence strengthened +{conf_change:.2f} since entry (now {current_confluence:.2f})"
            )
        conf_change = round(conf_change, 3)

    if current_momentum == "decreasing":
        live_score -= 10
        change_factors.append("Momentum is weakening")
        thesis_changed = True

    if current_volatility == "expanding":
        live_score -= 5
        change_factors.append("Volatility is expanding — stop-hit risk elevated")

    live_score = max(0.0, min(100.0, live_score))

    # ── Thesis status ─────────────────────────────────────────
    # Counter-regime shift → always INVALIDATED (takes highest priority)
    if regime_shift and _AFFINITY.get(
        ((current_regime or regime).lower(), side.lower()), 0
    ) == -1:
        thesis_status        = THESIS_INVALIDATED
        thesis_status_reason = (
            f"Regime has shifted to counter-regime for {side.upper()} direction."
        )
    elif live_score < 40:
        thesis_status        = THESIS_INVALIDATED
        thesis_status_reason = _summarise_changes(change_factors)
    elif live_score >= 80 and not thesis_changed:
        thesis_status        = THESIS_INTACT
        thesis_status_reason = "All entry conditions remain supportive."
    elif live_score >= 65 and not regime_shift and not thesis_changed:
        thesis_status        = THESIS_INTACT
        thesis_status_reason = "Entry conditions largely unchanged."
    elif live_score >= 50:
        thesis_status        = THESIS_WEAKENING
        thesis_status_reason = _summarise_changes(change_factors)
    else:
        thesis_status        = THESIS_WEAKENING
        thesis_status_reason = "Live conditions have deteriorated from entry quality."

    # Check for improved (positive drift)
    if live_score > 85 and (conf_change or 0) > 0.10:
        thesis_status        = THESIS_IMPROVED
        thesis_status_reason = "Confluence has strengthened since entry."

    # ── Disposition ───────────────────────────────────────────
    disp, disp_reason = _determine_disposition(
        live_score=live_score,
        thesis_status=thesis_status,
        regime_shift=regime_shift,
        current_regime=current_regime or regime,
        side=side,
        stop_set=stop_set,
        change_factors=change_factors,
    )

    # ── Live summary text ─────────────────────────────────────
    live_summary = _build_live_summary(
        live_score, thesis_status, disp, change_factors
    )

    return (
        live_score, live_summary, thesis_status, thesis_status_reason,
        thesis_changed, change_factors,
        regime_shift, conf_change,
        disp, disp_reason,
    )


def _determine_disposition(
    live_score: float, thesis_status: str,
    regime_shift: bool, current_regime: str,
    side: str, stop_set: bool,
    change_factors: list[str],
) -> tuple[str, str]:
    cur_aff = _AFFINITY.get((current_regime.lower(), side.lower()), 0)

    if thesis_status == THESIS_INVALIDATED or cur_aff == -1:
        return (
            DISP_EXIT_EARLY,
            "Thesis invalidated or regime shifted counter to position. "
            "Exit before stop is hit to preserve capital."
        )

    if not stop_set:
        return (
            DISP_HOLD_REDUCE,
            "No stop-loss set — reduce position size immediately to limit unlimited risk."
        )

    if thesis_status == THESIS_IMPROVED and live_score >= 80:
        return (
            DISP_PARTIAL_TP,
            "Thesis has strengthened since entry. Consider taking partial profit at the next resistance level."
        )

    if thesis_status == THESIS_WEAKENING and live_score < 55:
        return (
            DISP_TIGHTEN_STOP,
            "Conditions deteriorating. Tighten stop-loss toward breakeven to protect capital."
        )

    if thesis_status == THESIS_WEAKENING:
        return (
            DISP_HOLD_REDUCE,
            "Entry thesis is weakening. Reduce position size by 25–50% if deterioration continues."
        )

    # Default: thesis intact
    return (
        DISP_HOLD,
        "Entry thesis remains intact. Hold to original targets and let the system manage the exit."
    )


def _build_live_summary(
    live_score: float, thesis_status: str,
    disposition: str, change_factors: list[str]
) -> str:
    status_label = {
        THESIS_INTACT:      "intact",
        THESIS_WEAKENING:   "weakening",
        THESIS_INVALIDATED: "invalidated",
        THESIS_IMPROVED:    "strengthened",
    }.get(thesis_status, thesis_status)

    changes = (
        f" Changes detected: {'; '.join(change_factors[:3])}."
        if change_factors else ""
    )
    return (
        f"Trade thesis is {status_label} (live validity score {live_score:.0f}/100)."
        f"{changes} Recommended action: {disposition}."
    )


def _summarise_changes(factors: list[str]) -> str:
    if not factors:
        return "Conditions have changed since entry."
    return "; ".join(factors[:3]) + ("..." if len(factors) > 3 else ".")


# ── Empty fallback ────────────────────────────────────────────

def _empty_thesis() -> dict:
    return {
        "entry_thesis_summary":               "Thesis data unavailable.",
        "entry_signal_evidence":              [],
        "entry_regime_alignment_summary":     "Unknown.",
        "entry_htf_alignment_summary":        "Unknown.",
        "entry_risk_reward_summary":          "Unknown.",
        "entry_model_contribution_breakdown": [],
        "live_validity_summary":              "Live validity unavailable.",
        "live_validity_score":                50.0,
        "current_disposition":               DISP_HOLD,
        "current_disposition_reason":        "Insufficient data to determine disposition.",
        "thesis_changed_since_entry":         False,
        "thesis_change_factors":              [],
        "thesis_status":                      THESIS_INTACT,
        "thesis_status_reason":               "Insufficient data.",
        "regime_shift_detected":              False,
        "confluence_change_since_entry":      None,
        "momentum_change_since_entry":        None,
        "volatility_change_since_entry":      None,
        "liquidity_change_since_entry":       None,
    }
