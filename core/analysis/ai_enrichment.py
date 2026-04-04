# ============================================================
# NEXUS TRADER — AI Enrichment (Phase 2 — Multi-Mode)
#
# Calls deepseek-r1:14b via Ollama to produce natural-language
# analysis at different depths and for different channels.
#
# Modes:
#   ui_open_trade          — structured long-form open trade rationale
#   ui_closed_trade        — structured long-form post-trade review
#   notification_open      — compact 2-sentence open alert
#   notification_closed    — compact 2-sentence close alert
#   post_trade_review      — full multi-section review narrative
#
# Invariants:
#   • LLM may only EXPLAIN deterministic evidence — never invent new causes
#   • All prompts include the scoring result and root causes as grounding
#   • Falls back gracefully if Ollama is unavailable
#   • Never blocks notification dispatch (always async)
# ============================================================
from __future__ import annotations

import json
import logging
import threading
from typing import Optional, Callable

logger = logging.getLogger(__name__)

_OLLAMA_BASE  = "http://localhost:11434/v1"
_MODEL        = "deepseek-r1:14b"
_TIMEOUT_S    = 45

# ── Mode constants ────────────────────────────────────────────
MODE_UI_OPEN        = "ui_open_trade"
MODE_UI_CLOSED      = "ui_closed_trade"
MODE_NOTIF_OPEN     = "notification_open"
MODE_NOTIF_CLOSED   = "notification_closed"
MODE_POST_REVIEW    = "post_trade_review"


# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────

def enrich_async(
    trade:           dict,
    scoring_result:  dict,
    root_causes:     list[dict],
    recommendations: list[dict],
    on_complete:     Optional[Callable[[str], None]] = None,
    mode:            str = MODE_UI_CLOSED,
) -> None:
    """Launch AI enrichment in a background daemon thread."""
    def _worker():
        explanation = _call_ollama(trade, scoring_result, root_causes, recommendations, mode)
        if explanation and on_complete:
            try:
                on_complete(explanation)
            except Exception as exc:
                logger.debug("ai_enrichment on_complete callback failed: %s", exc)

    t = threading.Thread(target=_worker, daemon=True, name=f"ai-enrich-{mode}")
    t.start()


def enrich_sync(
    trade:           dict,
    scoring_result:  dict,
    root_causes:     list[dict],
    recommendations: list[dict],
    mode:            str = MODE_UI_CLOSED,
) -> Optional[str]:
    """Synchronous enrichment — blocks caller. For test use only."""
    return _call_ollama(trade, scoring_result, root_causes, recommendations, mode)


def enrich_open_trade_async(
    trade:          dict,
    analysis:       dict,
    on_complete:    Optional[Callable[[str], None]] = None,
    notification_mode: bool = False,
) -> None:
    """Convenience wrapper for open trade enrichment."""
    mode = MODE_NOTIF_OPEN if notification_mode else MODE_UI_OPEN
    enrich_async(
        trade=trade,
        scoring_result=analysis,
        root_causes=analysis.get("root_causes") or [],
        recommendations=analysis.get("recommendations") or [],
        on_complete=on_complete,
        mode=mode,
    )


# ─────────────────────────────────────────────────────────────
# Prompt builders — one per mode
# ─────────────────────────────────────────────────────────────

def _build_prompt(
    trade: dict,
    scoring_result: dict,
    root_causes: list[dict],
    recommendations: list[dict],
    mode: str,
) -> str:
    sym    = trade.get("symbol", "?")
    side   = (trade.get("side") or "buy").upper()
    regime = trade.get("regime", "unknown")
    score  = scoring_result.get("overall_score", 0.0)
    cls_   = scoring_result.get("classification", "NEUTRAL")
    pnl    = float(trade.get("pnl_usdt") or trade.get("pnl") or 0.0)
    models = ", ".join(trade.get("models_fired") or []) or "none"
    confluence = float(trade.get("score") or 0.0)
    htf    = trade.get("htf_confirmation")

    rc_text = "; ".join(
        f"{rc.get('category','?')}[{rc.get('severity','?')}]"
        for rc in root_causes[:5]
    ) or "none"

    rec_text = "; ".join(r.get("action","")[:60] for r in recommendations[:3]) or "none"

    evidence_constraint = (
        "\nIMPORTANT: Base your analysis ONLY on the evidence provided above. "
        "Do NOT invent root causes or reasons not supported by the data."
    )

    # ── Per-mode prompts ──────────────────────────────────────

    if mode == MODE_UI_OPEN:
        thesis = (scoring_result.get("thesis") or {})
        return (
            f"You are NexusTrader's trade coach. Write a structured analysis for an open position.\n\n"
            f"Trade: {sym} {side} | Regime: {regime} | Confluence: {confluence:.2f} | Models: {models}\n"
            f"HTF confirmed: {htf} | Setup score: {scoring_result.get('setup_score',0):.0f}/100 | "
            f"Risk score: {scoring_result.get('risk_score',0):.0f}/100\n"
            f"Root causes: {rc_text}\n"
            f"Live validity score: {thesis.get('live_validity_score', '?')}\n"
            f"Current disposition: {thesis.get('current_disposition', '?')}\n\n"
            f"Write a structured analysis in exactly 4 sections:\n"
            f"1. WHY OPENED (1 sentence: what evidence justified this trade)\n"
            f"2. WHAT SUPPORTS IT (1-2 sentences: strongest evidence for this setup)\n"
            f"3. WHAT RISKS EXIST (1 sentence: key risks right now)\n"
            f"4. WHAT TO DO NOW (1 sentence: concrete actionable recommendation)\n\n"
            f"Format: Use section labels exactly as shown. No markdown. Be concise and specific."
            f"{evidence_constraint}"
        )

    elif mode == MODE_UI_CLOSED:
        return (
            f"You are NexusTrader's trade coach. Write a post-trade review for a closed position.\n\n"
            f"Trade: {sym} {side} | Regime: {regime} | PnL: {pnl:+.2f} USDT\n"
            f"Quality: {score:.0f}/100 ({cls_}) | Confluence: {confluence:.2f} | Models: {models}\n"
            f"Root causes: {rc_text}\n"
            f"Top recommendations: {rec_text}\n\n"
            f"Write a structured review in exactly 4 sections:\n"
            f"1. TRADE SUMMARY (1 sentence: what happened and the key outcome)\n"
            f"2. WHAT WENT RIGHT (1 sentence: best aspect of this trade)\n"
            f"3. WHAT WENT WRONG (1 sentence: primary failure point, if any)\n"
            f"4. KEY LESSON (1 sentence: the single most important takeaway)\n\n"
            f"Format: Use section labels exactly as shown. No markdown. Be direct and specific."
            f"{evidence_constraint}"
        )

    elif mode == MODE_NOTIF_OPEN:
        return (
            f"You are NexusTrader's trade coach. Write a 2-sentence trade alert summary for an open position.\n\n"
            f"Trade: {sym} {side} | Regime: {regime} | Confluence: {confluence:.2f} | "
            f"Setup score: {scoring_result.get('setup_score',0):.0f}/100\n"
            f"Root causes: {rc_text}\n\n"
            f"Write exactly 2 sentences: sentence 1 = why this trade was opened; "
            f"sentence 2 = primary risk or note for the operator.\n"
            f"Plain text only, no labels, no markdown."
            f"{evidence_constraint}"
        )

    elif mode == MODE_NOTIF_CLOSED:
        return (
            f"You are NexusTrader's trade coach. Write a 2-sentence close alert summary.\n\n"
            f"Trade: {sym} {side} | PnL: {pnl:+.2f} USDT | Quality: {score:.0f}/100 ({cls_})\n"
            f"Root causes: {rc_text}\n\n"
            f"Write exactly 2 sentences: sentence 1 = what happened and outcome; "
            f"sentence 2 = primary lesson or risk note.\n"
            f"Plain text only, no labels, no markdown."
            f"{evidence_constraint}"
        )

    elif mode == MODE_POST_REVIEW:
        forensics = scoring_result.get("forensics") or {}
        matrix    = forensics.get("decision_outcome_matrix_label", "?")
        prev      = forensics.get("preventability_score", 0)
        return (
            f"You are NexusTrader's trade analyst. Write a thorough post-trade review.\n\n"
            f"Trade: {sym} {side} | Regime: {regime} | PnL: {pnl:+.2f} USDT\n"
            f"Quality: {score:.0f}/100 ({cls_}) | Confluence: {confluence:.2f} | Models: {models}\n"
            f"Decision matrix: {matrix} | Preventability: {prev:.0f}/100\n"
            f"Root causes: {rc_text}\n"
            f"Recommendations: {rec_text}\n\n"
            f"Write a thorough 5-section review:\n"
            f"1. TRADE OVERVIEW: what this trade was and what happened (2 sentences)\n"
            f"2. DECISION QUALITY: was the decision process sound? (2 sentences)\n"
            f"3. KEY FAILURE POINTS: what specifically went wrong (2 sentences)\n"
            f"4. WHAT COULD IMPROVE: concrete operational improvement (2 sentences)\n"
            f"5. TAKEAWAY: single most important lesson for future trades (1 sentence)\n\n"
            f"Format: Use section labels exactly as shown. No markdown. Be rigorous and specific."
            f"{evidence_constraint}"
        )

    else:
        # Fallback: compact 3-sentence
        return (
            f"You are NexusTrader's trade coach. Analyse this trade and write 3-sentence feedback.\n\n"
            f"Trade: {sym} {side} | Regime: {regime} | PnL: {pnl:+.2f} USDT | "
            f"Score: {score:.0f}/100 ({cls_})\n"
            f"Root causes: {rc_text}\n"
            f"Recommendations: {rec_text}\n\n"
            f"Response: exactly 3 plain-text sentences. Most important lesson first."
            f"{evidence_constraint}"
        )


# ─────────────────────────────────────────────────────────────
# Ollama call
# ─────────────────────────────────────────────────────────────

def _call_ollama(
    trade:           dict,
    scoring_result:  dict,
    root_causes:     list[dict],
    recommendations: list[dict],
    mode:            str = MODE_UI_CLOSED,
) -> Optional[str]:
    """
    Makes a completion call to the local Ollama instance.
    Returns plain-text explanation or None on any failure.
    """
    try:
        import urllib.request

        prompt  = _build_prompt(trade, scoring_result, root_causes, recommendations, mode)
        payload = json.dumps({
            "model":       _MODEL,
            "messages":    [{"role": "user", "content": prompt}],
            "stream":      False,
            "temperature": 0.3,   # low temperature for deterministic grounding
        }).encode("utf-8")

        req = urllib.request.Request(
            f"{_OLLAMA_BASE}/chat/completions",
            data    = payload,
            headers = {"Content-Type": "application/json"},
            method  = "POST",
        )

        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            body = json.loads(resp.read().decode("utf-8"))

        text = (
            body.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                .strip()
        )

        # Strip any <think> sections that deepseek-r1 may emit
        if "<think>" in text:
            import re
            text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

        if text:
            logger.debug(
                "ai_enrichment[%s]: received %d chars from Ollama", mode, len(text)
            )
            return text

        return None

    except Exception as exc:
        logger.debug(
            "ai_enrichment[%s]: Ollama unavailable or failed — %s", mode, exc
        )
        return None
