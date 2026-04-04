# ============================================================
# NEXUS TRADER — Canonical Rendering Pipeline (Phase 2)
#
# Single source of truth for how analysis objects are presented
# across ALL output channels. No channel may implement its own
# scoring, classification, root-cause, or recommendation logic.
#
# All channels call render_for_channel(analysis, channel, mode).
# Every channel produces output from the SAME canonical object.
#
# Supported modes:
#   ui_open_trade         — full analyst review panel (open)
#   ui_closed_trade       — full analyst review panel (closed)
#   notification_open     — compact notification (all channels)
#   notification_closed   — compact notification (all channels)
#   post_trade_review     — detailed review report text
#   email_open            — richer HTML-friendly block (open)
#   email_closed          — richer HTML-friendly block (closed)
#
# Invariant: same analysis object always → same classification,
#            scores, root causes, and recommendations across channels.
# ============================================================
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ── Mode constants ────────────────────────────────────────────
MODE_UI_OPEN           = "ui_open_trade"
MODE_UI_CLOSED         = "ui_closed_trade"
MODE_NOTIF_OPEN        = "notification_open"
MODE_NOTIF_CLOSED      = "notification_closed"
MODE_POST_REVIEW       = "post_trade_review"
MODE_EMAIL_OPEN        = "email_open"
MODE_EMAIL_CLOSED      = "email_closed"

# ── Score bar ────────────────────────────────────────────────
def _bar(score: float, width: int = 20) -> str:
    """ASCII progress bar for a 0–100 score."""
    filled = round(score / 100 * width)
    return "█" * filled + "░" * (width - filled)


def _score_label(score: float) -> str:
    if score >= 80:   return "EXCELLENT"
    if score >= 70:   return "GOOD"
    if score >= 55:   return "FAIR"
    if score >= 40:   return "POOR"
    return "CRITICAL"


def _rr_str(rr: Optional[float]) -> str:
    return f"{rr:.2f}:1" if rr is not None else "—"


def _affinity_label(a: int) -> str:
    return {1: "Aligned ✓", 0: "Neutral", -1: "Counter ✗"}.get(a, "?")


def _pct(v: float) -> str:
    return f"{v:+.2f}%"


def _fmt_pnl(pnl: float) -> str:
    sign = "+" if pnl >= 0 else ""
    return f"{sign}{pnl:.2f} USDT"


# ─────────────────────────────────────────────────────────────
# Master render dispatcher
# ─────────────────────────────────────────────────────────────

def render_for_channel(
    analysis: dict,
    mode: str,
    trade: Optional[dict] = None,
) -> dict:
    """
    Render the canonical analysis object for a specific output channel.

    Parameters
    ----------
    analysis : dict
        Output of TradeAnalysisService.build_trade_analysis()
    mode : str
        One of the MODE_* constants.
    trade : dict | None
        Original trade dict (for PnL, symbol, etc. in rendered text).

    Returns
    -------
    dict
        Channel-specific rendered payload. All payloads include:
          - classification    (str)
          - overall_score     (float)
          - root_causes       (list[dict])
          - recommendations   (list[dict])
          - text_lines        (list[str])  — plain-text lines for UI
          - summary_line      (str)        — single-line summary
        Plus channel-specific keys.
    """
    if mode in (MODE_UI_OPEN, MODE_UI_CLOSED):
        return _render_ui(analysis, mode, trade)
    elif mode in (MODE_NOTIF_OPEN, MODE_NOTIF_CLOSED):
        return _render_notification(analysis, mode, trade)
    elif mode in (MODE_EMAIL_OPEN, MODE_EMAIL_CLOSED):
        return _render_email(analysis, mode, trade)
    elif mode == MODE_POST_REVIEW:
        return _render_post_review(analysis, trade)
    else:
        logger.warning("canonical_renderer: unknown mode '%s', falling back to UI closed", mode)
        return _render_ui(analysis, MODE_UI_CLOSED, trade)


# ─────────────────────────────────────────────────────────────
# Core canonical field extraction (used by all renderers)
# ─────────────────────────────────────────────────────────────

def extract_canonical_fields(analysis: dict) -> dict:
    """
    Extract and validate the canonical fields from an analysis object.
    These fields MUST be consistent across all channels — never recomputed.
    """
    return {
        "classification":         analysis.get("classification",        "NEUTRAL"),
        "classification_emoji":   analysis.get("classification_emoji",  "⚖️"),
        "overall_score":          float(analysis.get("overall_score",   0.0)),
        "setup_score":            float(analysis.get("setup_score",     0.0)),
        "risk_score":             float(analysis.get("risk_score",      0.0)),
        "execution_score":        float(analysis.get("execution_score", 0.0)),
        "decision_score":         float(analysis.get("decision_score",  0.0)),
        "rr_ratio":               analysis.get("rr_ratio"),
        "regime_affinity":        int(analysis.get("regime_affinity",   0)),
        "hard_overrides":         analysis.get("hard_overrides")        or [],
        "root_causes":            analysis.get("root_causes")           or [],
        "recommendations":        analysis.get("recommendations")       or [],
        "penalty_log":            analysis.get("penalty_log")           or {},
        "is_open":                bool(analysis.get("is_open",          False)),
        "ai_explanation":         analysis.get("ai_explanation"),
        # Phase 2 fields
        "thesis":                 analysis.get("thesis"),
        "forensics":              analysis.get("forensics"),
        "regime_confidence_at_entry": float(analysis.get("regime_confidence_at_entry", 0.0)),
        "htf_confirmed_at_entry": analysis.get("htf_confirmed_at_entry"),
    }


# ─────────────────────────────────────────────────────────────
# UI renderer (full analyst review panel)
# ─────────────────────────────────────────────────────────────

def _render_ui(analysis: dict, mode: str, trade: Optional[dict]) -> dict:
    c = extract_canonical_fields(analysis)
    lines: list[str] = []
    t = trade or {}

    sym  = t.get("symbol", "")
    side = (t.get("side") or "").upper()

    if mode == MODE_UI_OPEN:
        lines += _ui_open_sections(c, t)
    else:
        lines += _ui_closed_sections(c, t)

    summary_line = _make_summary_line(c, t)

    return {
        **c,
        "text_lines":    lines,
        "summary_line":  summary_line,
        "mode":          mode,
    }


def _ui_open_sections(c: dict, t: dict) -> list[str]:
    """
    8-section open trade panel:
    1. Trade Summary
    2. Why This Trade Was Opened
    3. Signal Evidence
    4. Risk Assessment
    5. Live Validity
    6. Current Recommendation
    7. Watch Items / Warning Flags
    8. Learning Notes
    """
    lines = []
    thesis = c.get("thesis") or {}

    # ── Section 1: Trade Summary ──────────────────────────────
    lines += ["━━ TRADE SUMMARY ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"]
    sym   = t.get("symbol", "—")
    side  = (t.get("side") or "—").upper()
    entry = t.get("entry_price", "—")
    lines.append(f"  Symbol  : {sym}  {side}")
    lines.append(f"  Entry   : {entry}")
    lines.append(f"  Stop    : {t.get('stop_loss', '—')}")
    lines.append(f"  Target  : {t.get('take_profit', '—')}")
    lines.append(f"  R:R     : {_rr_str(c['rr_ratio'])}")
    lines.append(f"  Quality : {c['overall_score']:.0f}/100 {_bar(c['overall_score'], 16)} [{_score_label(c['overall_score'])}]")
    lines.append("")

    # ── Section 2: Why This Trade Was Opened ─────────────────
    lines += ["━━ WHY THIS TRADE WAS OPENED ━━━━━━━━━━━━━━━━━━━"]
    summary = thesis.get("entry_thesis_summary") or "Entry data unavailable."
    lines.append(f"  {summary}")
    lines.append("")
    lines.append(f"  {thesis.get('entry_regime_alignment_summary', '')}")
    lines.append(f"  {thesis.get('entry_htf_alignment_summary', '')}")
    lines.append("")

    # ── Section 3: Signal Evidence ────────────────────────────
    lines += ["━━ SIGNAL EVIDENCE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"]
    for ev in (thesis.get("entry_signal_evidence") or []):
        icon = "✓" if ev.get("positive") else "✗"
        lines.append(f"  {icon} {ev.get('label', '')}  —  {ev.get('detail', '')}")
    lines.append("")

    # ── Section 4: Risk Assessment ────────────────────────────
    lines += ["━━ RISK ASSESSMENT ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"]
    lines.append(f"  Setup Score   : {c['setup_score']:.0f}/100  {_bar(c['setup_score'], 16)}")
    lines.append(f"  Risk Score    : {c['risk_score']:.0f}/100  {_bar(c['risk_score'], 16)}")
    lines.append(f"  {thesis.get('entry_risk_reward_summary', '')}")
    if c["hard_overrides"]:
        for h in c["hard_overrides"]:
            lines.append(f"  ⚠ {h}")
    lines.append("")

    # ── Section 5: Live Validity ──────────────────────────────
    lines += ["━━ LIVE VALIDITY ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"]
    lv_score = thesis.get("live_validity_score", 0)
    lv_status = thesis.get("thesis_status", "unknown")
    lines.append(f"  Status  : {lv_status.upper()}  (score {lv_score:.0f}/100 {_bar(lv_score, 12)})")
    lines.append(f"  Summary : {thesis.get('live_validity_summary', '—')}")
    for factor in (thesis.get("thesis_change_factors") or [])[:3]:
        lines.append(f"  • {factor}")
    lines.append("")

    # ── Section 6: Current Recommendation ────────────────────
    lines += ["━━ CURRENT RECOMMENDATION ━━━━━━━━━━━━━━━━━━━━━━"]
    disp   = thesis.get("current_disposition", "Hold")
    reason = thesis.get("current_disposition_reason", "—")
    lines.append(f"  ACTION : {disp.upper()}")
    lines.append(f"  Reason : {reason}")
    lines.append("")

    # ── Section 7: Warning Flags ──────────────────────────────
    lines += ["━━ WATCH ITEMS / WARNING FLAGS ━━━━━━━━━━━━━━━━━"]
    warnings = _extract_warnings(c, thesis)
    if warnings:
        for w in warnings:
            lines.append(f"  ⚠ {w}")
    else:
        lines.append("  No active warnings.")
    lines.append("")

    # ── Section 8: Learning Notes ─────────────────────────────
    lines += ["━━ LEARNING NOTES ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"]
    recs = c.get("recommendations") or []
    for rec in recs[:2]:
        lines.append(f"  • [{rec.get('priority','?').upper()}] {rec.get('action','')[:100]}")
    if not recs:
        lines.append("  No specific learning notes for this trade.")
    lines.append("")

    return lines


def _ui_closed_sections(c: dict, t: dict) -> list[str]:
    """
    9-section closed trade panel:
    1. Trade Summary
    2. Outcome Classification
    3. Decision vs Outcome Matrix
    4. Scorecard
    5. What Went Right / Wrong
    6. Root Cause Analysis
    7. Preventability Assessment
    8. Improvement Suggestions
    9. Learning Impact
    """
    lines = []
    forensics = c.get("forensics") or {}

    pnl_usdt = float(t.get("pnl_usdt") or t.get("pnl") or 0.0)
    pnl_pct  = float(t.get("pnl_pct")  or 0.0)

    # ── Section 1: Trade Summary ──────────────────────────────
    lines += ["━━ TRADE SUMMARY ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"]
    lines.append(f"  Symbol   : {t.get('symbol','—')}  {(t.get('side') or '').upper()}")
    lines.append(f"  Entry    : {t.get('entry_price','—')}  →  Exit: {t.get('exit_price','—')}")
    lines.append(f"  PnL      : {_fmt_pnl(pnl_usdt)} ({_pct(pnl_pct)})")
    lines.append(f"  Duration : {_fmt_duration(int(t.get('duration_s') or 0))}")
    lines.append(f"  Exit via : {t.get('exit_reason','—')}")
    lines.append(f"  R:R      : {_rr_str(c['rr_ratio'])}")
    lines.append("")

    # ── Section 2: Outcome Classification ────────────────────
    lines += ["━━ OUTCOME CLASSIFICATION ━━━━━━━━━━━━━━━━━━━━━━"]
    cls_  = c["classification"]
    emoji = c["classification_emoji"]
    lines.append(f"  {emoji} {cls_}  (overall {c['overall_score']:.0f}/100 {_bar(c['overall_score'], 16)})")
    if c["hard_overrides"]:
        lines.append(f"  Hard overrides active — forced BAD:")
        for h in c["hard_overrides"]:
            lines.append(f"    ⚠ {h}")
    lines.append("")

    # ── Section 3: Decision vs Outcome Matrix ─────────────────
    lines += ["━━ DECISION vs OUTCOME MATRIX ━━━━━━━━━━━━━━━━━━"]
    matrix = forensics.get("decision_outcome_matrix_label", "—")
    _MATRIX_LABELS = {
        "GOOD_DECISION_GOOD_OUTCOME":  "✅ Good decision → Good outcome",
        "GOOD_DECISION_BAD_OUTCOME":   "⚖️ Good decision → Bad outcome (probabilistically acceptable)",
        "BAD_DECISION_BAD_OUTCOME":    "❌ Bad decision → Bad outcome (preventable)",
        "BAD_DECISION_LUCKY_OUTCOME":  "⚠️ Bad decision → Lucky outcome (unsustainable)",
    }
    lines.append(f"  {_MATRIX_LABELS.get(matrix, matrix)}")

    avoidable = forensics.get("avoidable_loss_flag") or forensics.get("avoidable_win_flag")
    if forensics.get("avoidable_loss_flag"):
        lines.append("  ⚠ AVOIDABLE LOSS: decision process violated system rules.")
    if forensics.get("avoidable_win_flag"):
        lines.append("  ⚠ AVOIDABLE WIN: profitable but taken against system rules.")
    if forensics.get("was_loss_probabilistically_acceptable"):
        lines.append("  ✓ Loss was within expected statistical range for this setup quality.")
    if forensics.get("was_win_quality_supported"):
        lines.append("  ✓ Win was supported by sound process and setup quality.")
    lines.append("")

    # ── Section 4: Scorecard ──────────────────────────────────
    lines += ["━━ SCORECARD ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"]
    for label, key in [
        ("Setup    ", "setup_score"),
        ("Risk     ", "risk_score"),
        ("Execution", "execution_score"),
        ("Decision ", "decision_score"),
    ]:
        s = c[key]
        lines.append(f"  {label}  {s:5.1f}/100  {_bar(s, 16)}  [{_score_label(s)}]")
    lines.append(f"  {'Overall  ':9s}  {c['overall_score']:5.1f}/100  {_bar(c['overall_score'], 16)}")
    lines.append("")

    # ── Section 5: What Went Right / Wrong ────────────────────
    lines += ["━━ WHAT WENT RIGHT / WRONG ━━━━━━━━━━━━━━━━━━━━━"]
    right, wrong = _split_right_wrong(c)
    if right:
        for item in right:
            lines.append(f"  ✓ {item}")
    if wrong:
        for item in wrong:
            lines.append(f"  ✗ {item}")
    if not right and not wrong:
        lines.append("  Insufficient data for right/wrong breakdown.")
    lines.append("")

    # ── Section 6: Root Cause Analysis ───────────────────────
    lines += ["━━ ROOT CAUSE ANALYSIS ━━━━━━━━━━━━━━━━━━━━━━━━━"]
    rcs = c.get("root_causes") or []
    if rcs:
        for rc in rcs[:5]:
            sev_icon = {"critical": "🔴", "major": "🟡", "minor": "⚪"}.get(rc.get("severity",""), "•")
            lines.append(f"  {sev_icon} [{rc.get('severity','?').upper()}] {rc.get('category','?')}")
            lines.append(f"     {rc.get('description','')}")
    else:
        lines.append("  No root causes identified.")
    lines.append("")

    # ── Section 7: Preventability Assessment ─────────────────
    lines += ["━━ PREVENTABILITY ASSESSMENT ━━━━━━━━━━━━━━━━━━━"]
    prev = forensics.get("preventability_score", 0)
    rand = forensics.get("randomness_score", 0)
    pdom = forensics.get("failure_domain_primary", "N/A")
    sdom = forensics.get("failure_domain_secondary", "N/A")
    lines.append(f"  Preventability : {prev:.0f}/100  {_bar(prev, 16)}")
    lines.append(f"  Randomness     : {rand:.0f}/100  {_bar(rand, 16)}")
    lines.append(f"  Primary domain : {pdom}")
    lines.append(f"  Secondary      : {sdom}")
    lines.append("")

    # ── Section 8: Improvement Suggestions ───────────────────
    lines += ["━━ IMPROVEMENT SUGGESTIONS ━━━━━━━━━━━━━━━━━━━━━"]
    recs = c.get("recommendations") or []
    if recs:
        for rec in recs[:3]:
            safe = "✓ Auto-tune safe" if rec.get("auto_tune_safe") else "✗ Manual review required"
            lines.append(f"  [{rec.get('priority','?').upper()}] {rec.get('action','')[:90]}")
            lines.append(f"  → {safe}")
    else:
        lines.append("  No specific improvements identified.")
    lines.append("")

    # ── Section 9: Learning Impact ────────────────────────────
    lines += ["━━ LEARNING IMPACT ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"]
    ai_exp = c.get("ai_explanation")
    if ai_exp:
        lines.append(f"  AI Coach: {ai_exp[:200]}")
    else:
        # Derive from recs
        if recs:
            lines.append(f"  Top improvement: {recs[0].get('rationale','')[:150]}")
        else:
            lines.append("  Learning analysis pending.")
    lines.append("")

    return lines


# ─────────────────────────────────────────────────────────────
# Notification renderer (compact, channel-safe)
# ─────────────────────────────────────────────────────────────

def _render_notification(analysis: dict, mode: str, trade: Optional[dict]) -> dict:
    """Compact notification payload consumed by all push channels."""
    c = extract_canonical_fields(analysis)
    t = trade or {}

    thesis    = c.get("thesis")    or {}
    forensics = c.get("forensics") or {}

    cls_  = c["classification"]
    emoji = c["classification_emoji"]
    score = c["overall_score"]
    rcs   = c.get("root_causes")    or []
    recs  = c.get("recommendations") or []

    pnl_usdt = float(t.get("pnl_usdt") or t.get("pnl") or 0.0)
    pnl_pct  = float(t.get("pnl_pct")  or 0.0)
    sym      = t.get("symbol", "?")
    side     = (t.get("side") or "?").upper()

    if mode == MODE_NOTIF_OPEN:
        top_rc  = rcs[0].get("category", "none") if rcs else "none"
        top_rec = recs[0].get("action", "")[:80] if recs else "—"
        disp    = thesis.get("current_disposition", "Hold")
        lv      = thesis.get("live_validity_score", 0)

        text_lines = [
            f"{emoji} {cls_} ENTRY — {sym} {side}",
            f"Quality: {score:.0f}/100  R:R: {_rr_str(c['rr_ratio'])}",
            f"Evidence strength: {thesis.get('entry_thesis_summary', '—')[:80]}",
            f"Live validity: {lv:.0f}/100  →  {disp}",
        ]
        if top_rc != "none":
            text_lines.append(f"Risk note: {top_rc}")

    else:  # NOTIF_CLOSED
        matrix  = forensics.get("decision_outcome_matrix_label", "—")
        avoidable = forensics.get("avoidable_loss_flag")
        top_rc  = rcs[0].get("category", "none") if rcs else "none"
        top_rec = recs[0].get("action", "")[:80] if recs else "—"
        prev    = forensics.get("preventability_score", 0)

        text_lines = [
            f"{emoji} {cls_} CLOSE — {sym} {side}  {_fmt_pnl(pnl_usdt)} ({_pct(pnl_pct)})",
            f"Quality: {score:.0f}/100  Matrix: {matrix}",
            f"Top root cause: {top_rc}",
            f"Preventability: {prev:.0f}/100",
            f"Improvement: {top_rec}",
        ]
        if avoidable:
            text_lines.append("⚠ Avoidable loss — system rules were not followed.")

    summary_line = text_lines[0] if text_lines else ""

    return {
        **c,
        "text_lines":    text_lines,
        "summary_line":  summary_line,
        "mode":          mode,
        # Flat notification keys (email/push template vars)
        "notif_classification":    cls_,
        "notif_emoji":             emoji,
        "notif_score":             f"{score:.1f}",
        "notif_rr":                _rr_str(c["rr_ratio"]),
        "notif_top_root_cause":    rcs[0].get("category", "—") if rcs else "—",
        "notif_top_recommendation":top_rec,
        "notif_pnl":               _fmt_pnl(pnl_usdt),
        "notif_pnl_pct":           _pct(pnl_pct),
        "notif_matrix":            forensics.get("decision_outcome_matrix_label", "—"),
        "notif_avoidable":         str(forensics.get("avoidable_loss_flag", False)),
        "notif_disposition":       thesis.get("current_disposition", "—"),
    }


# ─────────────────────────────────────────────────────────────
# Email renderer (richer structured block)
# ─────────────────────────────────────────────────────────────

def _render_email(analysis: dict, mode: str, trade: Optional[dict]) -> dict:
    """Email-friendly payload with extended analysis blocks."""
    base = _render_notification(
        analysis,
        MODE_NOTIF_OPEN if mode == MODE_EMAIL_OPEN else MODE_NOTIF_CLOSED,
        trade
    )
    c = extract_canonical_fields(analysis)
    t = trade or {}

    thesis    = c.get("thesis")    or {}
    forensics = c.get("forensics") or {}
    recs      = c.get("recommendations") or []
    rcs       = c.get("root_causes")     or []

    email_sections = {}

    if mode == MODE_EMAIL_OPEN:
        email_sections["why_opened"]     = thesis.get("entry_thesis_summary", "")
        email_sections["signal_evidence"]= [ev.get("label","") + ": " + ev.get("detail","")
                                             for ev in (thesis.get("entry_signal_evidence") or [])]
        email_sections["risk_summary"]   = thesis.get("entry_risk_reward_summary", "")
        email_sections["live_validity"]  = thesis.get("live_validity_summary", "")
        email_sections["disposition"]    = thesis.get("current_disposition", "Hold")
    else:
        email_sections["matrix"]         = forensics.get("decision_outcome_matrix_label","—")
        email_sections["scorecard"]      = {
            "setup":     c["setup_score"],
            "risk":      c["risk_score"],
            "execution": c["execution_score"],
            "decision":  c["decision_score"],
            "overall":   c["overall_score"],
        }
        email_sections["root_causes"]    = [rc.get("category","") + ": " + rc.get("description","")
                                             for rc in rcs[:3]]
        email_sections["preventability"] = forensics.get("preventability_score", 0)
        email_sections["avoidable"]      = forensics.get("avoidable_loss_flag", False)

    email_sections["recommendations"] = [r.get("action","")[:100] for r in recs[:2]]
    email_sections["ai_explanation"]  = c.get("ai_explanation", "")

    return {
        **base,
        "email_sections": email_sections,
        "mode":           mode,
    }


# ─────────────────────────────────────────────────────────────
# Post-trade review (detailed narrative)
# ─────────────────────────────────────────────────────────────

def _render_post_review(analysis: dict, trade: Optional[dict]) -> dict:
    c = extract_canonical_fields(analysis)
    t = trade or {}
    thesis    = c.get("thesis")    or {}
    forensics = c.get("forensics") or {}
    rcs       = c.get("root_causes")     or []
    recs      = c.get("recommendations") or []

    sections = {
        "trade_identity":        f"{t.get('symbol','?')} {(t.get('side') or '').upper()} @ {t.get('entry_price','?')}",
        "outcome_summary":       f"{c['classification_emoji']} {c['classification']} | Score {c['overall_score']:.0f}/100",
        "why_opened":            thesis.get("entry_thesis_summary", ""),
        "signal_evidence":       thesis.get("entry_signal_evidence") or [],
        "regime_summary":        thesis.get("entry_regime_alignment_summary", ""),
        "risk_summary":          thesis.get("entry_risk_reward_summary", ""),
        "decision_matrix":       forensics.get("decision_outcome_matrix_label", ""),
        "preventability":        forensics.get("preventability_score", 0),
        "randomness":            forensics.get("randomness_score", 0),
        "root_causes":           rcs,
        "recommendations":       recs,
        "ai_explanation":        c.get("ai_explanation") or "",
        "penalty_log":           c.get("penalty_log") or {},
    }

    lines = [
        f"POST-TRADE REVIEW: {sections['trade_identity']}",
        f"Outcome: {sections['outcome_summary']}",
        "",
        f"Why opened: {sections['why_opened']}",
        f"Regime: {sections['regime_summary']}",
        f"Risk: {sections['risk_summary']}",
        "",
        f"Decision matrix: {sections['decision_matrix']}",
        f"Preventability {sections['preventability']:.0f}/100  |  Randomness {sections['randomness']:.0f}/100",
        "",
        "Root causes:",
    ] + [f"  [{rc.get('severity','?').upper()}] {rc.get('category','?')}: {rc.get('description','')}"
         for rc in rcs[:5]] + [
        "",
        "Top recommendation:",
        f"  {recs[0].get('action','None') if recs else 'None'}",
        "",
        f"AI Coach: {sections['ai_explanation'] or '(pending)'}",
    ]

    return {
        **c,
        "sections":    sections,
        "text_lines":  lines,
        "summary_line":sections["outcome_summary"],
        "mode":        MODE_POST_REVIEW,
    }


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _make_summary_line(c: dict, t: dict) -> str:
    sym  = t.get("symbol", "?")
    side = (t.get("side") or "?").upper()
    cls_ = c["classification"]
    emoji= c["classification_emoji"]
    score= c["overall_score"]
    return f"{emoji} {cls_} | {sym} {side} | Score {score:.0f}/100"


def _fmt_duration(duration_s: int) -> str:
    if duration_s < 60:
        return f"{duration_s}s"
    if duration_s < 3600:
        return f"{duration_s // 60}m {duration_s % 60}s"
    h = duration_s // 3600
    m = (duration_s % 3600) // 60
    return f"{h}h {m}m"


def _extract_warnings(c: dict, thesis: dict) -> list[str]:
    """Extract active warning flags for the open trade panel."""
    warnings = []
    for h in c.get("hard_overrides") or []:
        warnings.append(f"HARD OVERRIDE: {h}")
    thesis_status = thesis.get("thesis_status", "")
    if thesis_status == "invalidated":
        warnings.append("Thesis INVALIDATED — exit early recommended.")
    elif thesis_status == "weakening":
        warnings.append("Thesis weakening — monitor closely.")
    if thesis.get("regime_shift_detected"):
        warnings.append("Regime shift detected since entry.")
    if c.get("regime_affinity") == -1:
        warnings.append("Counter-regime entry — elevated adverse probability.")
    return warnings


def _split_right_wrong(c: dict) -> tuple[list[str], list[str]]:
    """Split scoring evidence into 'went right' vs 'went wrong' items."""
    right: list[str] = []
    wrong: list[str] = []

    penalty_log = c.get("penalty_log") or {}
    scores = {
        "setup":     c["setup_score"],
        "risk":      c["risk_score"],
        "execution": c["execution_score"],
        "decision":  c["decision_score"],
    }
    for dim, score in scores.items():
        if score >= 80:
            right.append(f"{dim.capitalize()} quality was strong ({score:.0f}/100)")

    for dim, entries in penalty_log.items():
        for entry in entries[:2]:  # top 2 per dimension
            code = entry.split(":")[0] if ":" in entry else entry
            wrong.append(f"{dim.capitalize()}: {code.replace('_', ' ').lower()}")

    return right[:4], wrong[:5]
