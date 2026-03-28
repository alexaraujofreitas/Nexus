# ============================================================
# NEXUS TRADER — Notification Templates
#
# Structured message formatters for all notification types.
# Produces consistent, information-dense messages for each
# channel (WhatsApp, Telegram, SMS, Email).
#
# Template types:
#   trade_opened, trade_closed, trade_stopped, trade_rejected,
#   trade_modified, strategy_signal, risk_warning,
#   market_condition, system_error, system_alert, daily_summary
# ============================================================
from __future__ import annotations

import html as _html_mod
from datetime import datetime, timezone
from typing import Optional


# ── Emoji helpers ─────────────────────────────────────────────
_DIR_EMOJI   = {"long": "📈", "short": "📉", "buy": "📈", "sell": "📉"}
_RISK_EMOJI  = {"low": "🟢", "medium": "🟡", "high": "🔴", "critical": "🚨"}
_REG_EMOJI   = {
    "bull_trend": "🐂", "bear_trend": "🐻", "ranging": "↔️",
    "volatility_expansion": "⚡", "volatility_compression": "🗜️", "uncertain": "❓",
}


def _fmt_price(v, decimals: int = 4) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):,.{decimals}f}"
    except (TypeError, ValueError):
        return str(v)


def _fmt_pct(v) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):+.2f}%"
    except (TypeError, ValueError):
        return str(v)


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _dir(direction: str) -> str:
    d = direction.upper() if direction else "—"
    emoji = _DIR_EMOJI.get(direction.lower(), "◈") if direction else "◈"
    return f"{emoji} {d}"


# ── Template functions ────────────────────────────────────────

def trade_opened(data: dict) -> dict[str, str]:
    """
    Returns {'subject': ..., 'body': ..., 'short': ..., 'html_body': ...}
    data keys: symbol, direction, entry_price, size, stop_loss, take_profit,
               strategy, confidence, rationale, timeframe, regime
               + optional analysis_* keys from TradeAnalysisService
    """
    sym       = data.get("symbol", "???")
    direction = data.get("direction", "long")
    entry     = _fmt_price(data.get("entry_price"))
    size      = data.get("size", "—")
    sl        = _fmt_price(data.get("stop_loss"))
    tp        = _fmt_price(data.get("take_profit"))
    strategy  = data.get("strategy", "—")
    conf      = data.get("confidence", 0.0)
    rationale = data.get("rationale", "")
    tf        = data.get("timeframe", "—")
    regime    = data.get("regime", "—")
    reg_emoji = _REG_EMOJI.get(regime, "◈")

    # ── Analysis enrichment ───────────────────────────────────
    a_overall  = data.get("analysis_overall", "")
    a_setup    = data.get("analysis_setup", "")
    a_risk     = data.get("analysis_risk", "")
    a_rr       = data.get("analysis_rr", "—")
    a_cls      = data.get("analysis_classification", "")
    a_emoji    = data.get("analysis_emoji", "")
    a_rc       = data.get("analysis_root_causes", "")

    analysis_block = ""
    if a_overall:
        analysis_block = (
            f"{'─'*42}\n"
            f"  AI ANALYSIS:  {a_emoji} {a_cls}  (Overall: {a_overall}/100)\n"
            f"  Setup: {a_setup}  Risk: {a_risk}  R:R: {a_rr}\n"
        )
        if a_rc and a_rc != "None identified":
            analysis_block += f"  Watch: {a_rc}\n"

    subject = f"🚀 Trade Opened — {sym} {direction.upper()}"

    body = (
        f"{'='*42}\n"
        f"  NEXUS TRADER — TRADE OPENED\n"
        f"{'='*42}\n"
        f"  Asset:       {sym}\n"
        f"  Direction:   {_dir(direction)}\n"
        f"  Entry:       {entry}\n"
        f"  Size:        {size}\n"
        f"  Stop Loss:   {sl}\n"
        f"  Take Profit: {tp}\n"
        f"{'─'*42}\n"
        f"  Strategy:    {strategy}\n"
        f"  Confidence:  {conf:.0%}\n"
        f"  Timeframe:   {tf}\n"
        f"  Regime:      {reg_emoji} {regime}\n"
        f"{'─'*42}\n"
        f"  Rationale:\n"
        f"  {rationale}\n"
        f"{analysis_block}"
        f"{'─'*42}\n"
        f"  Time:        {_now_utc()}\n"
        f"{'='*42}"
    )

    short = (
        f"🚀 *{sym}* {_dir(direction)} @ {entry}\n"
        f"SL: {sl} | TP: {tp}\n"
        f"Strategy: {strategy} | Conf: {conf:.0%}"
    )
    if a_emoji and a_cls:
        short += f"\nQuality: {a_emoji} {a_cls} ({a_overall}/100)"

    result: dict = {"subject": subject, "body": body, "short": short}

    # ── Rich HTML email body ───────────────────────────────────
    try:
        result["html_body"] = _build_trade_opened_html(data)
    except Exception:
        pass  # graceful fallback — email will use <pre> wrapper

    return result


def trade_closed(data: dict) -> dict[str, str]:
    """
    data keys: symbol, direction, entry_price, exit_price, pnl, pnl_pct,
               size, strategy, close_reason, duration
               + optional analysis_* keys from TradeAnalysisService
    """
    sym        = data.get("symbol", "???")
    direction  = data.get("direction", "long")
    entry      = _fmt_price(data.get("entry_price"))
    exit_p     = _fmt_price(data.get("exit_price"))
    pnl        = data.get("pnl", 0.0)
    pnl_pct    = data.get("pnl_pct", 0.0)
    size       = data.get("size", "—")
    strategy   = data.get("strategy", "—")
    reason     = data.get("close_reason", "manual")
    duration   = data.get("duration", "—")

    pnl_sign  = "✅" if float(pnl or 0) >= 0 else "❌"
    pnl_str   = f"{pnl_sign} {_fmt_pct(pnl_pct)} ({'+' if float(pnl or 0)>=0 else ''}{_fmt_price(pnl, 2)} USDT)"

    # ── Analysis enrichment ───────────────────────────────────
    a_overall  = data.get("analysis_overall", "")
    a_setup    = data.get("analysis_setup", "")
    a_risk     = data.get("analysis_risk", "")
    a_exec     = data.get("analysis_execution", "")
    a_decision = data.get("analysis_decision", "")
    a_rr       = data.get("analysis_rr", "—")
    a_cls      = data.get("analysis_classification", "")
    a_emoji    = data.get("analysis_emoji", "")
    a_rc       = data.get("analysis_root_causes", "")
    a_rec      = data.get("analysis_recommendation", "")

    analysis_block = ""
    if a_overall:
        analysis_block = (
            f"{'─'*42}\n"
            f"  AI ANALYSIS:  {a_emoji} {a_cls}  (Overall: {a_overall}/100)\n"
            f"  Setup: {a_setup}  Risk: {a_risk}  Exec: {a_exec}  Decision: {a_decision}\n"
            f"  R:R: {a_rr}\n"
        )
        if a_rc and a_rc != "None identified":
            analysis_block += f"  Root Causes: {a_rc}\n"
        if a_rec and a_rec != "No specific recommendations.":
            analysis_block += f"  Recommendation: {a_rec}\n"

    subject = f"{pnl_sign} Trade Closed — {sym} | {_fmt_pct(pnl_pct)}"
    if a_emoji and a_cls:
        subject += f" | {a_emoji} {a_cls}"

    body = (
        f"{'='*42}\n"
        f"  NEXUS TRADER — TRADE CLOSED\n"
        f"{'='*42}\n"
        f"  Asset:       {sym}\n"
        f"  Direction:   {_dir(direction)}\n"
        f"  Entry:       {entry}\n"
        f"  Exit:        {exit_p}\n"
        f"  Size:        {size}\n"
        f"  P&L:         {pnl_str}\n"
        f"{'─'*42}\n"
        f"  Strategy:    {strategy}\n"
        f"  Closed By:   {reason}\n"
        f"  Duration:    {duration}\n"
        f"{analysis_block}"
        f"{'─'*42}\n"
        f"  Time:        {_now_utc()}\n"
        f"{'='*42}"
    )

    short = (
        f"{pnl_sign} *{sym}* closed @ {exit_p}\n"
        f"Entry: {entry} | P&L: {pnl_str}\n"
        f"Reason: {reason}"
    )
    if a_emoji and a_cls:
        short += f"\nQuality: {a_emoji} {a_cls} ({a_overall}/100)"

    result: dict = {"subject": subject, "body": body, "short": short}

    # ── Rich HTML email body ───────────────────────────────────
    try:
        result["html_body"] = _build_trade_closed_html(data)
    except Exception:
        pass

    return result


def trade_stopped(data: dict) -> dict[str, str]:
    """Stop-loss or forced liquidation hit."""
    sym       = data.get("symbol", "???")
    direction = data.get("direction", "long")
    entry     = _fmt_price(data.get("entry_price"))
    stop      = _fmt_price(data.get("stop_price"))
    loss      = data.get("loss", 0.0)
    loss_pct  = data.get("loss_pct", 0.0)

    subject = f"🛑 Stop-Loss Hit — {sym}"

    body = (
        f"{'='*42}\n"
        f"  NEXUS TRADER — STOP-LOSS HIT\n"
        f"{'='*42}\n"
        f"  Asset:      {sym}\n"
        f"  Direction:  {_dir(direction)}\n"
        f"  Entry:      {entry}\n"
        f"  Stop Hit:   {stop}\n"
        f"  Loss:       ❌ {_fmt_pct(loss_pct)} ({_fmt_price(loss, 2)} USDT)\n"
        f"{'─'*42}\n"
        f"  Time:       {_now_utc()}\n"
        f"{'='*42}"
    )

    short = (
        f"🛑 *{sym}* stop-loss hit @ {stop}\n"
        f"Loss: {_fmt_pct(loss_pct)} | Entry was {entry}"
    )

    return {"subject": subject, "body": body, "short": short}


def trade_rejected(data: dict) -> dict[str, str]:
    """Signal was rejected by risk gate or confluence scorer."""
    sym      = data.get("symbol", "???")
    strategy = data.get("strategy", "—")
    reason   = data.get("reason", "risk gate")
    conf     = data.get("confidence", 0.0)
    regime   = data.get("regime", "—")

    subject = f"⚠️ Signal Rejected — {sym}"

    body = (
        f"{'='*42}\n"
        f"  NEXUS TRADER — SIGNAL REJECTED\n"
        f"{'='*42}\n"
        f"  Asset:      {sym}\n"
        f"  Strategy:   {strategy}\n"
        f"  Confidence: {conf:.0%}\n"
        f"  Regime:     {regime}\n"
        f"  Reason:     {reason}\n"
        f"{'─'*42}\n"
        f"  Time:       {_now_utc()}\n"
        f"{'='*42}"
    )

    short = f"⚠️ *{sym}* signal rejected — {reason}"

    return {"subject": subject, "body": body, "short": short}


def trade_modified(data: dict) -> dict[str, str]:
    """SL/TP or position size was modified (e.g., trailing stop update)."""
    sym      = data.get("symbol", "???")
    change   = data.get("change_description", "parameters updated")
    old_sl   = _fmt_price(data.get("old_stop_loss"))
    new_sl   = _fmt_price(data.get("new_stop_loss"))
    old_tp   = _fmt_price(data.get("old_take_profit"))
    new_tp   = _fmt_price(data.get("new_take_profit"))

    subject = f"🔧 Trade Modified — {sym}"

    body = (
        f"{'='*42}\n"
        f"  NEXUS TRADER — TRADE MODIFIED\n"
        f"{'='*42}\n"
        f"  Asset:      {sym}\n"
        f"  Change:     {change}\n"
        f"  SL:         {old_sl} → {new_sl}\n"
        f"  TP:         {old_tp} → {new_tp}\n"
        f"{'─'*42}\n"
        f"  Time:       {_now_utc()}\n"
        f"{'='*42}"
    )

    short = f"🔧 *{sym}* modified — {change}"

    return {"subject": subject, "body": body, "short": short}


def strategy_signal(data: dict) -> dict[str, str]:
    """Strong strategy signal found (pre-trade alert)."""
    sym       = data.get("symbol", "???")
    direction = data.get("direction", "long")
    strategy  = data.get("strategy", "—")
    conf      = data.get("confidence", 0.0)
    regime    = data.get("regime", "—")
    reg_emoji = _REG_EMOJI.get(regime, "◈")
    signals   = data.get("contributing_signals", [])
    entry     = _fmt_price(data.get("entry_price"))
    sl        = _fmt_price(data.get("stop_loss"))
    tp        = _fmt_price(data.get("take_profit"))

    sigs_str = ", ".join(signals[:5]) if signals else "—"

    subject = f"💡 Signal Alert — {sym} {direction.upper()}"

    body = (
        f"{'='*42}\n"
        f"  NEXUS TRADER — STRATEGY SIGNAL\n"
        f"{'='*42}\n"
        f"  Asset:       {sym}\n"
        f"  Direction:   {_dir(direction)}\n"
        f"  Strategy:    {strategy}\n"
        f"  Confidence:  {conf:.0%}\n"
        f"  Regime:      {reg_emoji} {regime}\n"
        f"{'─'*42}\n"
        f"  Entry:       {entry}\n"
        f"  Stop Loss:   {sl}\n"
        f"  Take Profit: {tp}\n"
        f"{'─'*42}\n"
        f"  Signals:     {sigs_str}\n"
        f"  Time:        {_now_utc()}\n"
        f"{'='*42}"
    )

    short = (
        f"💡 *{sym}* {_dir(direction)} signal\n"
        f"Strategy: {strategy} | Conf: {conf:.0%}\n"
        f"Entry: {entry} | SL: {sl} | TP: {tp}"
    )

    return {"subject": subject, "body": body, "short": short}


def risk_warning(data: dict) -> dict[str, str]:
    """Risk threshold crossed (drawdown, position limit, etc.)."""
    warning_type = data.get("warning_type", "Risk Warning")
    level        = data.get("level", "high")
    message      = data.get("message", "Risk threshold exceeded")
    current_val  = data.get("current_value", "—")
    threshold    = data.get("threshold", "—")
    risk_emoji   = _RISK_EMOJI.get(level, "⚠️")

    subject = f"{risk_emoji} Risk Warning — {warning_type}"

    body = (
        f"{'='*42}\n"
        f"  NEXUS TRADER — RISK WARNING\n"
        f"{'='*42}\n"
        f"  Type:       {warning_type}\n"
        f"  Level:      {risk_emoji} {level.upper()}\n"
        f"  Message:    {message}\n"
        f"  Current:    {current_val}\n"
        f"  Threshold:  {threshold}\n"
        f"{'─'*42}\n"
        f"  Time:       {_now_utc()}\n"
        f"{'='*42}"
    )

    short = f"{risk_emoji} *Risk Warning* — {warning_type}\n{message}"

    return {"subject": subject, "body": body, "short": short}


def market_condition(data: dict) -> dict[str, str]:
    """Abnormal market condition detected (regime change, vol spike, etc.)."""
    condition = data.get("condition", "Market Alert")
    regime    = data.get("regime", "—")
    reg_emoji = _REG_EMOJI.get(regime, "◈")
    message   = data.get("message", "")
    confidence= data.get("confidence", 0.0)

    subject = f"🌐 Market Alert — {condition}"

    body = (
        f"{'='*42}\n"
        f"  NEXUS TRADER — MARKET CONDITION\n"
        f"{'='*42}\n"
        f"  Condition:  {condition}\n"
        f"  Regime:     {reg_emoji} {regime}\n"
        f"  Confidence: {confidence:.0%}\n"
        f"  Details:    {message}\n"
        f"{'─'*42}\n"
        f"  Time:       {_now_utc()}\n"
        f"{'='*42}"
    )

    short = f"🌐 *Market Alert* — {condition}\n{message}"

    return {"subject": subject, "body": body, "short": short}


def system_error(data: dict) -> dict[str, str]:
    """Critical system error that needs attention."""
    component = data.get("component", "System")
    error     = data.get("error", "Unknown error")
    severity  = data.get("severity", "error")

    subject = f"🚨 System Error — {component}"

    body = (
        f"{'='*42}\n"
        f"  NEXUS TRADER — SYSTEM ERROR\n"
        f"{'='*42}\n"
        f"  Component:  {component}\n"
        f"  Severity:   {severity.upper()}\n"
        f"  Error:      {error}\n"
        f"{'─'*42}\n"
        f"  Time:       {_now_utc()}\n"
        f"{'='*42}"
    )

    short = f"🚨 *System Error* — {component}\n{error}"

    return {"subject": subject, "body": body, "short": short}


def system_alert(data: dict) -> dict[str, str]:
    """General system alert / informational notice."""
    title   = data.get("title", "System Alert")
    message = data.get("message", "")

    subject = f"ℹ️ {title}"

    body = (
        f"{'='*42}\n"
        f"  NEXUS TRADER — SYSTEM ALERT\n"
        f"{'='*42}\n"
        f"  {title}\n"
        f"{'─'*42}\n"
        f"  {message}\n"
        f"{'─'*42}\n"
        f"  Time:       {_now_utc()}\n"
        f"{'='*42}"
    )

    short = f"ℹ️ *{title}*\n{message}"

    return {"subject": subject, "body": body, "short": short}


def emergency_stop(data: dict) -> dict[str, str]:
    """Emergency stop activated — all positions being closed."""
    reason   = data.get("reason", "emergency stop triggered")
    open_pos = data.get("open_positions", 0)
    equity   = _fmt_price(data.get("equity"), 2)

    subject = "🚨 EMERGENCY STOP ACTIVATED"

    body = (
        f"{'='*42}\n"
        f"  NEXUS TRADER — ‼️ EMERGENCY STOP ‼️\n"
        f"{'='*42}\n"
        f"  Reason:          {reason}\n"
        f"  Open Positions:  {open_pos} being closed\n"
        f"  Equity:          {equity} USDT\n"
        f"{'─'*42}\n"
        f"  ALL TRADING HALTED — Review required\n"
        f"{'─'*42}\n"
        f"  Time:            {_now_utc()}\n"
        f"{'='*42}"
    )

    short = f"🚨 *EMERGENCY STOP* — {reason}\n{open_pos} positions being closed"

    return {"subject": subject, "body": body, "short": short}


def crash_defensive(data: dict) -> dict[str, str]:
    """Tier 1 crash alert — defensive mode activated."""
    score   = data.get("score", 0.0)
    tier    = data.get("tier", "DEFENSIVE")
    actions = data.get("actions", "halt new longs")
    ts      = data.get("timestamp", _now_utc())

    subject = f"⚠️ CRASH ALERT — Defensive Mode (Score: {score:.1f}/10)"

    body = (
        f"{'='*42}\n"
        f"  NEXUS TRADER — ⚠️ CRASH ALERT\n"
        f"  Tier: DEFENSIVE | Score: {score:.1f}/10\n"
        f"{'='*42}\n"
        f"  Status: Defensive mode ACTIVATED\n"
        f"  Actions Taken:\n"
        f"  {actions}\n"
        f"{'─'*42}\n"
        f"  Recommendation: Monitor closely.\n"
        f"  No new long entries permitted.\n"
        f"{'─'*42}\n"
        f"  Time: {ts}\n"
        f"{'='*42}"
    )

    short = f"⚠️ *CRASH ALERT — DEFENSIVE*\nScore: {score:.1f}/10\nNew longs halted. Monitor closely."
    return {"subject": subject, "body": body, "short": short}


def crash_high_alert(data: dict) -> dict[str, str]:
    """Tier 2 crash alert — high alert, partial exit."""
    score   = data.get("score", 0.0)
    actions = data.get("actions", "closing 50% longs")
    ts      = data.get("timestamp", _now_utc())

    subject = f"🔴 CRASH ALERT — HIGH ALERT (Score: {score:.1f}/10)"

    body = (
        f"{'='*42}\n"
        f"  NEXUS TRADER — 🔴 HIGH ALERT\n"
        f"  Tier: HIGH_ALERT | Score: {score:.1f}/10\n"
        f"{'='*42}\n"
        f"  Status: PARTIAL EXIT INITIATED\n"
        f"  Actions Taken:\n"
        f"  {actions}\n"
        f"{'─'*42}\n"
        f"  50% of long positions being closed.\n"
        f"  Trailing stops activated.\n"
        f"{'─'*42}\n"
        f"  Time: {ts}\n"
        f"{'='*42}"
    )

    short = f"🔴 *CRASH ALERT — HIGH ALERT*\nScore: {score:.1f}/10\n50% longs closed. Trailing stops ON."
    return {"subject": subject, "body": body, "short": short}


def crash_emergency(data: dict) -> dict[str, str]:
    """Tier 3 crash alert — emergency, all longs closed."""
    score   = data.get("score", 0.0)
    actions = data.get("actions", "closing all longs")
    ts      = data.get("timestamp", _now_utc())

    subject = f"🚨 CRASH EMERGENCY — All Longs Closed (Score: {score:.1f}/10)"

    body = (
        f"{'='*42}\n"
        f"  NEXUS TRADER — 🚨 CRASH EMERGENCY\n"
        f"  Tier: EMERGENCY | Score: {score:.1f}/10\n"
        f"{'='*42}\n"
        f"  Status: ALL LONG POSITIONS CLOSED\n"
        f"  Actions Taken:\n"
        f"  {actions}\n"
        f"{'─'*42}\n"
        f"  System in READ-ONLY mode.\n"
        f"  No new trades until manual override.\n"
        f"{'─'*42}\n"
        f"  Time: {ts}\n"
        f"{'='*42}"
    )

    short = f"🚨 *CRASH EMERGENCY*\nScore: {score:.1f}/10\nALL LONGS CLOSED. READ-ONLY mode."
    return {"subject": subject, "body": body, "short": short}


def crash_systemic(data: dict) -> dict[str, str]:
    """Tier 4 crash alert — systemic crisis, all positions closed."""
    score   = data.get("score", 0.0)
    actions = data.get("actions", "closing all positions")
    ts      = data.get("timestamp", _now_utc())

    subject = f"‼️ SYSTEMIC CRISIS — ALL POSITIONS CLOSED (Score: {score:.1f}/10)"

    body = (
        f"{'='*42}\n"
        f"  NEXUS TRADER — ‼️ SYSTEMIC CRISIS\n"
        f"  Tier: SYSTEMIC | Score: {score:.1f}/10\n"
        f"{'='*42}\n"
        f"  Status: ALL POSITIONS CLOSED\n"
        f"  SAFE MODE ACTIVATED\n"
        f"  Actions Taken:\n"
        f"  {actions}\n"
        f"{'─'*42}\n"
        f"  TRADING HALTED — Manual restart required.\n"
        f"  Review market conditions before resuming.\n"
        f"{'─'*42}\n"
        f"  Time: {ts}\n"
        f"{'='*42}"
    )

    short = f"‼️ *SYSTEMIC CRISIS*\nScore: {score:.1f}/10\nALL POSITIONS CLOSED. SAFE MODE."
    return {"subject": subject, "body": body, "short": short}


def _build_health_html(data: dict) -> str:
    """
    Generate a dark-themed, rich HTML email body for health check notifications.
    Uses table-based layout for maximum email client compatibility (Gmail, Outlook, Apple Mail).
    All user-supplied string values are HTML-escaped.
    """

    # ── HTML escape helper ─────────────────────────────────────────────
    def _e(v) -> str:
        return _html_mod.escape(str(v)) if v is not None else ""

    # ── Status indicator (coloured dot) ────────────────────────────────
    def _sdot(s: str) -> str:
        sl = (s or "").lower()
        if any(w in sl for w in ("running", "active", "connected", "ok", "online")):
            return '<span style="color:#10B981;font-size:14px">●</span>'
        if any(w in sl for w in ("error", "fail", "down", "inactive", "disconnected", "stopped")):
            return '<span style="color:#EF4444;font-size:14px">●</span>'
        return '<span style="color:#F59E0B;font-size:14px">●</span>'

    # ── P&L helpers ────────────────────────────────────────────────────
    def _pnl_color(v: float) -> str:
        return "#10B981" if v >= 0 else "#EF4444"

    def _pfx(v: float) -> str:
        return "+" if v >= 0 else ""

    # ── Badge helpers ──────────────────────────────────────────────────
    def _side_badge(side: str) -> str:
        is_long = (side or "").lower() in ("buy", "long")
        bg = "#064E3B" if is_long else "#7F1D1D"
        fg = "#34D399" if is_long else "#FCA5A5"
        label = "LONG" if is_long else "SHORT"
        return (
            f'<span style="background:{bg};color:{fg};font-size:10px;font-weight:700;'
            f'padding:2px 7px;border-radius:3px;letter-spacing:1px">{label}</span>'
        )

    def _tf_badge(tf) -> str:
        return (
            f'<span style="background:#1E3A5F;color:#93C5FD;font-size:10px;font-weight:600;'
            f'padding:2px 5px;border-radius:3px">{_e(tf or "?")}</span>'
        )

    def _result_badge(won: bool) -> str:
        bg = "#064E3B" if won else "#7F1D1D"
        fg = "#34D399" if won else "#FCA5A5"
        label = "WIN" if won else "LOSS"
        return (
            f'<span style="background:{bg};color:{fg};font-size:10px;font-weight:700;'
            f'padding:2px 6px;border-radius:3px">{label}</span>'
        )

    # ── Format helpers ─────────────────────────────────────────────────
    def _fmt_models(mf) -> str:
        if not mf:
            return "—"
        if isinstance(mf, list):
            return _e(", ".join(str(m) for m in mf)) if mf else "—"
        return _e(str(mf))

    def _fmt_dur(s) -> str:
        try:
            s = int(s or 0)
        except Exception:
            return "—"
        if s <= 0:
            return "—"
        if s < 60:
            return f"{s}s"
        if s < 3600:
            return f"{s // 60}m {s % 60}s"
        h = s // 3600
        m = (s % 3600) // 60
        return f"{h}h {m}m"

    def _fmt_dt(iso_str) -> str:
        """Return 'YYYY-MM-DD HH:MM UTC' or '—'."""
        try:
            return str(iso_str)[:16].replace("T", " ") + " UTC"
        except Exception:
            return "—"

    def _fmt_p(v, dec: int = 2) -> str:
        """Format a price value."""
        try:
            return f"{float(v):,.{dec}f}"
        except Exception:
            return "—"

    def _price_dec(v: float) -> int:
        """Auto-select decimal places: 2 for large prices (>$100), 4 for small."""
        return 2 if v > 100 else 4

    # ── Data extraction ────────────────────────────────────────────────
    scanner      = data.get("scanner_status", "Unknown")
    last_scan    = data.get("last_scan_ago",  "Unknown")
    exchange     = data.get("exchange_status","Unknown")
    feed         = data.get("feed_status",    "Unknown")
    ai_status    = data.get("ai_status",      "Unknown")
    portfolio    = float(data.get("portfolio_value") or 0.0)
    cash         = float(data.get("available_cash")  or 0.0)
    today_pnl    = float(data.get("today_pnl")       or 0.0)
    today_pnl_pct= float(data.get("today_pnl_pct")   or 0.0)
    win_rate     = float(data.get("win_rate")         or 0.0)
    total_trades = int(  data.get("total_trades")     or 0)
    open_count   = int(  data.get("open_positions")   or 0)
    ts           = _now_utc()

    open_trades   = data.get("open_trades_detail")   or []
    closed_trades = data.get("closed_trades_detail") or []

    # ── Open position card ─────────────────────────────────────────────
    def _open_card(p: dict) -> str:
        sym     = _e(p.get("symbol", "???"))
        side    = p.get("side", "")
        tf      = p.get("timeframe", "?")
        ep      = float(p.get("entry_price")   or 0)
        cp      = float(p.get("current_price") or 0)
        upnl_p  = float(p.get("unrealized_pnl") or 0)   # already a percentage
        sz      = float(p.get("size_usdt")     or 0)
        upnl_u  = sz * upnl_p / 100                      # USDT P&L
        sl      = float(p.get("stop_loss")     or 0)
        tp      = float(p.get("take_profit")   or 0)
        score   = float(p.get("score")         or 0)
        regime  = _e(p.get("regime")           or "—")
        models  = _fmt_models(p.get("models_fired"))
        opened  = _fmt_dt(p.get("opened_at",   ""))
        bars    = p.get("bars_held")
        bars_str= f" &nbsp;·&nbsp; {bars} bars" if bars is not None else ""

        pc      = _pnl_color(upnl_p)
        dp      = _price_dec(ep)

        return (
            f'<tr><td style="padding:8px 0 0 0">'
            f'<div style="background:#0D1117;border:1px solid #1E2D40;'
            f'border-left:3px solid {pc};border-radius:6px;padding:14px 16px">'

            # ── Card header: symbol + badges + P&L ──────────────────
            f'<table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:10px">'
            f'<tr>'
            f'<td style="vertical-align:middle">'
            f'<span style="color:#E2E8F0;font-size:14px;font-weight:700">{sym}</span>'
            f'&nbsp;&nbsp;{_side_badge(side)}&nbsp;{_tf_badge(tf)}'
            f'</td>'
            f'<td align="right" style="vertical-align:middle">'
            f'<span style="color:{pc};font-size:14px;font-weight:700">'
            f'{_pfx(upnl_p)}{upnl_p:.2f}%&nbsp;({_pfx(upnl_u)}${abs(upnl_u):,.2f})'
            f'</span>'
            f'</td>'
            f'</tr>'
            f'</table>'

            # ── Card body: field grid ────────────────────────────────
            f'<table width="100%" cellpadding="0" cellspacing="4" style="font-size:12px;color:#6B7280">'
            f'<tr>'
            f'<td width="50%" style="padding-bottom:4px">'
            f'Entry &nbsp;<span style="color:#C8D0E0">${_fmt_p(ep, dp)}</span></td>'
            f'<td width="50%" style="padding-bottom:4px">'
            f'Current &nbsp;<span style="color:#C8D0E0">${_fmt_p(cp, dp)}</span></td>'
            f'</tr>'
            f'<tr>'
            f'<td style="padding-bottom:4px">'
            f'Size &nbsp;<span style="color:#C8D0E0">${sz:,.2f} USDT</span></td>'
            f'<td style="padding-bottom:4px">'
            f'Score &nbsp;<span style="color:#C8D0E0">{score:.2f}</span></td>'
            f'</tr>'
            f'<tr>'
            f'<td style="padding-bottom:4px">'
            f'SL &nbsp;<span style="color:#EF4444">${_fmt_p(sl, dp)}</span></td>'
            f'<td style="padding-bottom:4px">'
            f'TP &nbsp;<span style="color:#10B981">${_fmt_p(tp, dp)}</span></td>'
            f'</tr>'
            f'<tr>'
            f'<td colspan="2" style="padding-bottom:4px">'
            f'Regime &nbsp;<span style="color:#93C5FD">{regime}</span></td>'
            f'</tr>'
            f'<tr>'
            f'<td colspan="2" style="padding-bottom:4px">'
            f'Models &nbsp;<span style="color:#C8D0E0">{models}</span></td>'
            f'</tr>'
            f'<tr>'
            f'<td colspan="2">'
            f'Opened &nbsp;<span style="color:#C8D0E0">{_e(opened)}{bars_str}</span></td>'
            f'</tr>'
            f'</table>'
            f'</div>'
            f'</td></tr>'
        )

    # ── Closed trade card ──────────────────────────────────────────────
    def _closed_card(t: dict) -> str:
        sym    = _e(t.get("symbol", "???"))
        side   = t.get("side", "")
        tf     = t.get("timeframe", "?")
        ep     = float(t.get("entry_price") or 0)
        xp     = float(t.get("exit_price")  or 0)
        pnl_u  = float(t.get("pnl_usdt")   or 0)
        pnl_p  = float(t.get("pnl_pct")    or 0)
        exit_r = _e((t.get("exit_reason") or "—").replace("_", " "))
        score  = float(t.get("score")       or 0)
        regime = _e(t.get("regime")         or "—")
        models = _fmt_models(t.get("models_fired"))
        dur    = _fmt_dur(t.get("duration_s"))
        closed = _fmt_dt(t.get("closed_at", ""))
        won    = pnl_u >= 0
        pc     = _pnl_color(pnl_u)
        dp_ep  = _price_dec(ep)
        dp_xp  = _price_dec(xp)

        return (
            f'<tr><td style="padding:8px 0 0 0">'
            f'<div style="background:#0D1117;border:1px solid #1E2D40;'
            f'border-left:3px solid {pc};border-radius:6px;padding:14px 16px">'

            # ── Card header ──────────────────────────────────────────
            f'<table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:10px">'
            f'<tr>'
            f'<td style="vertical-align:middle">'
            f'<span style="color:#E2E8F0;font-size:14px;font-weight:700">{sym}</span>'
            f'&nbsp;&nbsp;{_side_badge(side)}&nbsp;{_tf_badge(tf)}'
            f'&nbsp;&nbsp;{_result_badge(won)}'
            f'</td>'
            f'<td align="right" style="vertical-align:middle">'
            f'<span style="color:{pc};font-size:14px;font-weight:700">'
            f'{_pfx(pnl_u)}${abs(pnl_u):,.2f}&nbsp;({_pfx(pnl_p)}{pnl_p:.2f}%)'
            f'</span>'
            f'</td>'
            f'</tr>'
            f'</table>'

            # ── Card body ────────────────────────────────────────────
            f'<table width="100%" cellpadding="0" cellspacing="4" style="font-size:12px;color:#6B7280">'
            f'<tr>'
            f'<td width="50%" style="padding-bottom:4px">'
            f'Entry &nbsp;<span style="color:#C8D0E0">${_fmt_p(ep, dp_ep)}</span></td>'
            f'<td width="50%" style="padding-bottom:4px">'
            f'Exit &nbsp;<span style="color:#C8D0E0">${_fmt_p(xp, dp_xp)}</span></td>'
            f'</tr>'
            f'<tr>'
            f'<td style="padding-bottom:4px">'
            f'Duration &nbsp;<span style="color:#C8D0E0">{_e(dur)}</span></td>'
            f'<td style="padding-bottom:4px">'
            f'Score &nbsp;<span style="color:#C8D0E0">{score:.2f}</span></td>'
            f'</tr>'
            f'<tr>'
            f'<td style="padding-bottom:4px">'
            f'Exit Reason &nbsp;<span style="color:#C8D0E0">{exit_r}</span></td>'
            f'<td style="padding-bottom:4px">'
            f'Regime &nbsp;<span style="color:#93C5FD">{regime}</span></td>'
            f'</tr>'
            f'<tr>'
            f'<td colspan="2" style="padding-bottom:4px">'
            f'Models &nbsp;<span style="color:#C8D0E0">{models}</span></td>'
            f'</tr>'
            f'<tr>'
            f'<td colspan="2">'
            f'Closed &nbsp;<span style="color:#C8D0E0">{_e(closed)}</span></td>'
            f'</tr>'
            f'</table>'
            f'</div>'
            f'</td></tr>'
        )

    # ── System status rows ─────────────────────────────────────────────
    status_rows = ""
    for lbl, val in [
        ("IDSS Scanner", f"{scanner} (last: {last_scan})"),
        ("Exchange",     exchange),
        ("Data Feed",    feed),
        ("AI Provider",  ai_status),
    ]:
        status_rows += (
            f'<tr>'
            f'<td style="color:#6B7280;font-size:12px;padding:5px 0;width:30%">{_e(lbl)}</td>'
            f'<td style="font-size:12px;padding:5px 0">{_sdot(val)}'
            f'&nbsp;<span style="color:#C8D0E0">{_e(val)}</span></td>'
            f'</tr>'
        )

    # ── Portfolio metrics ──────────────────────────────────────────────
    pc_pnl  = _pnl_color(today_pnl)
    pfx_pnl = _pfx(today_pnl)
    wr_lbl  = f"{win_rate:.1f}%" if total_trades > 0 else "—"

    def _metric_cell(label: str, value: str, sub: str = "", color: str = "#E2E8F0") -> str:
        sub_html = f'<div style="color:{color};font-size:10px;margin-top:2px">{sub}</div>' if sub else ""
        return (
            f'<td style="text-align:center;padding:10px 8px;background:#111827;border-radius:4px">'
            f'<div style="color:#6B7280;font-size:10px;margin-bottom:4px;letter-spacing:0.5px">'
            f'{_e(label)}</div>'
            f'<div style="color:{color};font-size:15px;font-weight:700">{value}</div>'
            f'{sub_html}'
            f'</td>'
        )

    # ── Open positions section ─────────────────────────────────────────
    if open_trades:
        open_cards_html = "".join(_open_card(p) for p in open_trades)
        open_section = (
            f'<tr><td style="padding-top:20px">'
            f'<div style="color:#6B7280;font-size:10px;font-weight:700;letter-spacing:1.5px;'
            f'margin-bottom:6px">OPEN POSITIONS ({len(open_trades)})</div>'
            f'<table width="100%" cellpadding="0" cellspacing="0">'
            f'{open_cards_html}'
            f'</table>'
            f'</td></tr>'
        )
    else:
        open_section = (
            f'<tr><td style="padding-top:20px">'
            f'<div style="color:#6B7280;font-size:10px;font-weight:700;letter-spacing:1.5px;'
            f'margin-bottom:8px">OPEN POSITIONS</div>'
            f'<div style="background:#0D1117;border:1px dashed #1E2D40;border-radius:6px;'
            f'padding:20px;text-align:center;color:#6B7280;font-size:12px">'
            f'No open positions</div>'
            f'</td></tr>'
        )

    # ── Closed trades section (most-recent first) ──────────────────────
    n_closed_shown = min(len(closed_trades), 10)
    if closed_trades:
        closed_cards_html = "".join(_closed_card(t) for t in reversed(closed_trades))
        closed_section = (
            f'<tr><td style="padding-top:20px">'
            f'<div style="color:#6B7280;font-size:10px;font-weight:700;letter-spacing:1.5px;'
            f'margin-bottom:6px">'
            f'RECENT CLOSED TRADES ({n_closed_shown} of {total_trades})</div>'
            f'<table width="100%" cellpadding="0" cellspacing="0">'
            f'{closed_cards_html}'
            f'</table>'
            f'</td></tr>'
        )
    else:
        closed_section = (
            f'<tr><td style="padding-top:20px">'
            f'<div style="color:#6B7280;font-size:10px;font-weight:700;letter-spacing:1.5px;'
            f'margin-bottom:8px">RECENT CLOSED TRADES</div>'
            f'<div style="background:#0D1117;border:1px dashed #1E2D40;border-radius:6px;'
            f'padding:20px;text-align:center;color:#6B7280;font-size:12px">'
            f'No closed trades yet</div>'
            f'</td></tr>'
        )

    # ── Separator between spacer cells ────────────────────────────────
    _GAP = '<td style="width:8px"></td>'

    # ── Assemble full HTML ─────────────────────────────────────────────
    return (
        '<!DOCTYPE html>'
        '<html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1.0">'
        '</head>'
        '<body style="margin:0;padding:0;background:#0A0E1A;'
        'font-family:Arial,Helvetica,sans-serif">'
        '<table width="100%" cellpadding="0" cellspacing="0" style="background:#0A0E1A">'
        '<tr><td align="center">'
        '<table width="680" cellpadding="0" cellspacing="0" '
        'style="max-width:680px;width:100%;padding:24px">'

        # Header ────────────────────────────────────────────────────────
        '<tr><td style="padding-bottom:20px">'
        '<table width="100%" cellpadding="0" cellspacing="0"><tr>'
        '<td style="border-left:4px solid #3B82F6;padding-left:12px;vertical-align:top">'
        '<div style="color:#3B82F6;font-size:10px;font-weight:700;letter-spacing:2px">NEXUSTRADER</div>'
        '<div style="color:#E2E8F0;font-size:20px;font-weight:700;margin-top:2px">System Health Check</div>'
        f'<div style="color:#6B7280;font-size:12px;margin-top:4px">{_e(ts)}</div>'
        '</td>'
        '<td align="right" style="vertical-align:top">'
        '<div style="background:#1E2D40;border-radius:6px;padding:10px 16px;display:inline-block;text-align:center">'
        '<div style="color:#6B7280;font-size:10px;margin-bottom:2px">TOTAL EQUITY</div>'
        f'<div style="color:#E2E8F0;font-size:20px;font-weight:700">${portfolio:,.2f}</div>'
        '<div style="color:#6B7280;font-size:10px">USDT (incl. open P&amp;L)</div>'
        '</div>'
        '</td>'
        '</tr></table>'
        '</td></tr>'

        # System Status ─────────────────────────────────────────────────
        '<tr><td>'
        '<div style="background:#0D1117;border:1px solid #1E2D40;border-radius:6px;padding:16px">'
        '<div style="color:#6B7280;font-size:10px;font-weight:700;letter-spacing:1.5px;'
        'margin-bottom:10px">SYSTEM STATUS</div>'
        f'<table width="100%" cellpadding="0" cellspacing="0">{status_rows}</table>'
        '</div>'
        '</td></tr>'

        # Portfolio Metrics ──────────────────────────────────────────────
        '<tr><td style="padding-top:12px">'
        '<div style="background:#0D1117;border:1px solid #1E2D40;border-radius:6px;padding:16px">'
        '<div style="color:#6B7280;font-size:10px;font-weight:700;letter-spacing:1.5px;'
        'margin-bottom:12px">PORTFOLIO</div>'
        '<table width="100%" cellpadding="0" cellspacing="0"><tr>'
        + _metric_cell("EQUITY", f"${portfolio:,.2f}", "USDT")
        + _GAP
        + _metric_cell("AVAILABLE", f"${cash:,.2f}", "USDT")
        + _GAP
        + _metric_cell("TODAY P&amp;L",
                        f"{pfx_pnl}${abs(today_pnl):,.2f}",
                        f"{pfx_pnl}{today_pnl_pct:.2f}%",
                        color=pc_pnl)
        + _GAP
        + _metric_cell("WIN RATE", wr_lbl, f"{total_trades} closed")
        + '</tr></table>'
        '</div>'
        '</td></tr>'

        # Open Positions + Closed Trades ────────────────────────────────
        + open_section
        + closed_section

        # Footer ────────────────────────────────────────────────────────
        + '<tr><td style="padding-top:24px;text-align:center">'
        '<div style="color:#374151;font-size:10px;border-top:1px solid #1E2D40;'
        'padding-top:16px">NexusTrader — Demo Mode &nbsp;·&nbsp; Paper Trading Only</div>'
        '</td></tr>'

        '</table>'
        '</td></tr></table>'
        '</body></html>'
    )


def health_check(data: dict) -> dict[str, str]:
    """
    System health check notification.
    data keys: scanner_status, last_scan_ago, exchange_status, feed_status,
               ai_status, portfolio_value, available_cash, today_pnl,
               today_pnl_pct, win_rate, total_trades, open_positions,
               open_trades_detail, closed_trades_detail, timestamp

    portfolio_value      = total equity (free cash + mark-to-market value of open positions)
    today_pnl            = realised P&L closed today + current unrealized P&L on open positions
    total_trades         = closed trades only (open positions are shown separately)
    open_trades_detail   = list[dict] from PaperPosition.to_dict() + augmented fields
    closed_trades_detail = list[dict] — last 10 closed trade dicts from _closed_trades
    """
    scanner      = data.get("scanner_status",  "Unknown")
    last_scan    = data.get("last_scan_ago",    "Unknown")
    exchange     = data.get("exchange_status", "Unknown")
    feed         = data.get("feed_status",     "Unknown")
    ai           = data.get("ai_status",       "Unknown")
    portfolio    = _fmt_price(data.get("portfolio_value"), 2)
    cash         = _fmt_price(data.get("available_cash"), 2)
    t_pnl        = float(data.get("today_pnl", 0.0) or 0.0)
    t_pnl_pct    = float(data.get("today_pnl_pct", 0.0) or 0.0)
    win_rate     = data.get("win_rate", 0.0)
    trades       = data.get("total_trades", 0)
    open_pos     = data.get("open_positions", 0)
    open_detail  = data.get("open_trades_detail")  or []
    closed_detail= data.get("closed_trades_detail") or []

    def _status_icon(s: str) -> str:
        s_lower = s.lower()
        if any(w in s_lower for w in ("running", "active", "connected", "ok", "online")):
            return "✅"
        if any(w in s_lower for w in ("error", "fail", "down", "inactive")):
            return "❌"
        return "⚠️"

    pnl_sign = "✅" if t_pnl >= 0 else "❌"
    pnl_prefix = "+" if t_pnl >= 0 else ""
    pnl_str  = f"{pnl_sign} {_fmt_pct(t_pnl_pct)} ({pnl_prefix}{_fmt_price(t_pnl, 2)} USDT)"

    # Closed trades stat — make clear this is completed trades only
    closed_label = f"{trades} closed"
    wr_label     = f"{win_rate:.1f}%" if trades > 0 else "n/a (no closed trades)"

    subject = f"💊 Health Check — Portfolio: {portfolio} USDT | P&L: {_fmt_pct(t_pnl_pct)}"

    # ── Plain-text body ────────────────────────────────────────────────
    # Open positions compact summary
    open_lines = ""
    if open_detail:
        open_lines = f"\n{'─'*42}\n  OPEN POSITIONS ({len(open_detail)})\n{'─'*42}\n"
        for p in open_detail:
            sym  = p.get("symbol", "???")
            side = ("LONG" if (p.get("side") or "").lower() in ("buy","long") else "SHORT")
            tf   = p.get("timeframe", "?")
            ep   = float(p.get("entry_price")   or 0)
            up   = float(p.get("unrealized_pnl") or 0)
            sz   = float(p.get("size_usdt")     or 0)
            uu   = sz * up / 100
            pfx  = "+" if up >= 0 else ""
            open_lines += f"  {sym} {side} {tf}  |  P&L: {pfx}{up:.2f}% ({pfx}${uu:,.2f})  |  Entry: ${ep:,.2f}  |  Size: ${sz:,.2f}\n"

    # Closed trades compact summary (most recent first, up to 5)
    closed_lines = ""
    recent_closed = list(reversed(closed_detail))[:5]
    if recent_closed:
        closed_lines = f"\n{'─'*42}\n  RECENT CLOSED TRADES\n{'─'*42}\n"
        for t in recent_closed:
            sym   = t.get("symbol", "???")
            side  = ("LONG" if (t.get("side") or "").lower() in ("buy","long") else "SHORT")
            tf    = t.get("timeframe", "?")
            pu    = float(t.get("pnl_usdt") or 0)
            pp    = float(t.get("pnl_pct")  or 0)
            xr    = (t.get("exit_reason") or "—").replace("_", " ")
            icon  = "✅" if pu >= 0 else "❌"
            pfx   = "+" if pu >= 0 else ""
            closed_lines += f"  {icon} {sym} {side} {tf}  |  {pfx}${pu:,.2f} ({pfx}{pp:.2f}%)  |  {xr}\n"

    body = (
        f"{'='*42}\n"
        f"  NEXUS TRADER — HEALTH CHECK\n"
        f"  {_now_utc()}\n"
        f"{'='*42}\n"
        f"  SYSTEM STATUS\n"
        f"{'─'*42}\n"
        f"  IDSS Scanner:  {_status_icon(scanner)} {scanner}\n"
        f"  Last Scan:     {last_scan}\n"
        f"  Exchange:      {_status_icon(exchange)} {exchange}\n"
        f"  Data Feed:     {_status_icon(feed)} {feed}\n"
        f"  AI Provider:   {_status_icon(ai)} {ai}\n"
        f"{'─'*42}\n"
        f"  PORTFOLIO\n"
        f"{'─'*42}\n"
        f"  Total Equity:  {portfolio} USDT  (incl. open P&L)\n"
        f"  Available:     {cash} USDT\n"
        f"  Open Positions:{open_pos}\n"
        f"{'─'*42}\n"
        f"  PERFORMANCE\n"
        f"{'─'*42}\n"
        f"  Today P&L:     {pnl_str}  (realised + unrealised)\n"
        f"  Win Rate:      {wr_label}\n"
        f"  Closed Trades: {closed_label}\n"
        + open_lines
        + closed_lines
        + f"{'='*42}"
    )

    short = (
        f"💊 *Health Check* — {_now_utc()}\n"
        f"{_status_icon(scanner)} Scanner: {scanner} (last scan: {last_scan}) | "
        f"{_status_icon(exchange)} Exchange: {exchange} | "
        f"{_status_icon(feed)} Feed: {feed}\n"
        f"Equity: {portfolio} USDT | Cash: {cash} USDT | Open: {open_pos}\n"
        f"P&L: {pnl_str} | WR: {wr_label} | Closed: {closed_label}"
    )

    # ── Rich HTML body (email channels) ───────────────────────────────
    try:
        html_body = _build_health_html(data)
    except Exception:
        html_body = None

    result = {"subject": subject, "body": body, "short": short}
    if html_body:
        result["html_body"] = html_body
    return result


def daily_summary(data: dict) -> dict[str, str]:
    """Daily performance summary."""
    date      = data.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    trades    = data.get("total_trades", 0)
    wins      = data.get("wins", 0)
    losses    = data.get("losses", 0)
    pnl       = data.get("daily_pnl", 0.0)
    pnl_pct   = data.get("daily_pnl_pct", 0.0)
    win_rate  = data.get("win_rate", 0.0)
    equity    = _fmt_price(data.get("equity"), 2)
    regime    = data.get("current_regime", "—")
    reg_emoji = _REG_EMOJI.get(regime, "◈")
    pnl_sign  = "✅" if float(pnl or 0) >= 0 else "❌"

    subject = f"📊 Daily Summary — {date} | P&L: {_fmt_pct(pnl_pct)}"

    body = (
        f"{'='*42}\n"
        f"  NEXUS TRADER — DAILY SUMMARY\n"
        f"  {date}\n"
        f"{'='*42}\n"
        f"  Total Trades:  {trades} ({wins}W / {losses}L)\n"
        f"  Win Rate:      {win_rate:.0%}\n"
        f"  Daily P&L:     {pnl_sign} {_fmt_pct(pnl_pct)} ({_fmt_price(pnl, 2)} USDT)\n"
        f"  Equity:        {equity} USDT\n"
        f"{'─'*42}\n"
        f"  Regime:        {reg_emoji} {regime}\n"
        f"{'─'*42}\n"
        f"  Time:          {_now_utc()}\n"
        f"{'='*42}"
    )

    short = (
        f"📊 *Daily Summary* — {date}\n"
        f"Trades: {trades} | Win Rate: {win_rate:.0%}\n"
        f"P&L: {pnl_sign} {_fmt_pct(pnl_pct)} | Equity: {equity}"
    )

    return {"subject": subject, "body": body, "short": short}


# ── HTML builders for trade notifications (Session 35) ────────

_TRADE_HTML_CSS = """
body{margin:0;padding:0;font-family:Arial,Helvetica,sans-serif;
     background:#080C16;color:#C8D0E0;}
.wrap{max-width:620px;margin:0 auto;padding:20px 16px;}
.card{background:#0D1320;border:1px solid #1A2332;border-radius:8px;
      margin-bottom:14px;overflow:hidden;}
.card-header{padding:12px 16px;border-bottom:1px solid #1A2332;}
.card-body{padding:14px 16px;}
.badge{display:inline-block;padding:2px 8px;border-radius:4px;
       font-size:11px;font-weight:700;letter-spacing:0.5px;}
.row{display:flex;justify-content:space-between;margin-bottom:6px;
     font-size:13px;}
.lbl{color:#8899AA;}
.val{color:#E8EBF0;font-weight:600;}
.green{color:#00CC77;}.red{color:#FF3355;}.amber{color:#FFB300;}
.blue{color:#1E90FF;}.muted{color:#8899AA;}
.score-row{display:flex;align-items:center;gap:8px;margin-bottom:8px;}
.score-bar-wrap{flex:1;height:6px;background:#141E2E;border-radius:3px;}
.score-bar{height:6px;border-radius:3px;}
.footer{text-align:center;font-size:11px;color:#4A6A8A;
        padding:10px 0 0;border-top:1px solid #1A2332;}
"""


def _score_bar_color(s: float) -> str:
    return "#00CC77" if s >= 75 else ("#FFB300" if s >= 55 else "#FF3355")


def _classification_badge_style(cls_: str) -> str:
    return {
        "GOOD":    "background:#0A3320;color:#00CC77;border:1px solid #00CC77;",
        "BAD":     "background:#3A0A14;color:#FF3355;border:1px solid #FF3355;",
        "NEUTRAL": "background:#1A2030;color:#FFB300;border:1px solid #FFB300;",
    }.get(cls_, "background:#1A2030;color:#8899AA;border:1px solid #8899AA;")


def _score_row_html(label: str, score: float) -> str:
    pct    = max(0, min(100, score))
    color  = _score_bar_color(pct)
    return (
        f'<div class="score-row">'
        f'<span class="lbl" style="min-width:80px;font-size:12px;">{_html_mod.escape(label)}</span>'
        f'<div class="score-bar-wrap">'
        f'<div class="score-bar" style="width:{pct:.0f}%;background:{color};"></div>'
        f'</div>'
        f'<span style="font-size:12px;font-weight:700;color:{color};min-width:40px;text-align:right;">'
        f'{score:.0f}</span>'
        f'</div>'
    )


def _build_trade_opened_html(data: dict) -> str:
    """Rich HTML body for trade_opened email."""
    e = _html_mod.escape
    sym       = e(data.get("symbol",    "???"))
    direction = (data.get("direction",  "long") or "long").upper()
    entry     = e(_fmt_price(data.get("entry_price")))
    size      = e(str(data.get("size", "—")))
    sl        = e(_fmt_price(data.get("stop_loss")))
    tp        = e(_fmt_price(data.get("take_profit")))
    strategy  = e(str(data.get("strategy", "—")))
    conf      = data.get("confidence", 0.0)
    tf        = e(str(data.get("timeframe", "—")))
    regime    = e(str(data.get("regime", "—")))
    rationale = e(str(data.get("rationale", "—")))
    timestamp = e(_now_utc())

    dir_color = "#00CC77" if direction in ("BUY", "LONG") else "#FF3355"

    # Analysis section
    a_overall  = data.get("analysis_overall", "")
    a_setup    = float(data.get("analysis_setup", 0) or 0)
    a_risk     = float(data.get("analysis_risk", 0) or 0)
    a_rr       = e(str(data.get("analysis_rr", "—")))
    a_cls      = data.get("analysis_classification", "")
    a_emoji    = data.get("analysis_emoji", "")
    a_rc       = e(str(data.get("analysis_root_causes", "") or ""))

    analysis_html = ""
    if a_overall:
        badge_style = _classification_badge_style(a_cls)
        analysis_html = f"""
        <div class="card">
          <div class="card-header">
            <span style="font-weight:700;font-size:13px;">🤖 AI Entry Quality</span>
            <span class="badge" style="{badge_style};float:right;">
              {e(str(a_emoji))} {e(str(a_cls))} &nbsp; {e(str(a_overall))}/100
            </span>
          </div>
          <div class="card-body">
            {_score_row_html("Setup", a_setup)}
            {_score_row_html("Risk", a_risk)}
            <div class="row" style="margin-top:8px;">
              <span class="lbl">R:R Ratio</span>
              <span class="val">{a_rr}</span>
            </div>
            {"<div class='row'><span class='lbl'>Watch</span>"
             f"<span class='val amber' style='max-width:320px;text-align:right;'>{a_rc}</span></div>"
             if a_rc and a_rc != "None identified" else ""}
          </div>
        </div>"""

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>{_TRADE_HTML_CSS}</style></head>
<body><div class="wrap">

<div class="card">
  <div class="card-header" style="background:#0A1830;">
    <span style="font-size:16px;font-weight:700;color:#E8EBF0;">🚀 Trade Opened</span>
    <span style="float:right;color:{dir_color};font-weight:700;">{sym} {direction}</span>
  </div>
  <div class="card-body">
    <div class="row"><span class="lbl">Entry Price</span><span class="val">{entry}</span></div>
    <div class="row"><span class="lbl">Position Size</span><span class="val">{size}</span></div>
    <div class="row"><span class="lbl">Stop Loss</span>
      <span class="val red">{sl}</span></div>
    <div class="row"><span class="lbl">Take Profit</span>
      <span class="val green">{tp}</span></div>
    <div class="row"><span class="lbl">Strategy</span><span class="val">{strategy}</span></div>
    <div class="row"><span class="lbl">Confluence</span>
      <span class="val">{conf:.0%}</span></div>
    <div class="row"><span class="lbl">Timeframe</span><span class="val">{tf}</span></div>
    <div class="row"><span class="lbl">Regime</span><span class="val">{regime}</span></div>
  </div>
</div>

<div class="card">
  <div class="card-header">
    <span style="font-weight:700;font-size:13px;">📝 Rationale</span>
  </div>
  <div class="card-body" style="font-size:13px;line-height:1.6;">{rationale}</div>
</div>

{analysis_html}

<div class="footer">NexusTrader • {timestamp}</div>
</div></body></html>"""

    return html


def _build_trade_closed_html(data: dict) -> str:
    """Rich HTML body for trade_closed email including full AI quality scorecard."""
    e = _html_mod.escape
    sym       = e(data.get("symbol",    "???"))
    direction = (data.get("direction",  "long") or "long").upper()
    entry     = e(_fmt_price(data.get("entry_price")))
    exit_p    = e(_fmt_price(data.get("exit_price")))
    size      = e(str(data.get("size", "—")))
    strategy  = e(str(data.get("strategy", "—")))
    reason    = e(str(data.get("close_reason", "—")))
    duration  = e(str(data.get("duration", "—")))
    timestamp = e(_now_utc())

    pnl       = float(data.get("pnl", 0.0) or 0.0)
    pnl_pct   = float(data.get("pnl_pct", 0.0) or 0.0)
    pnl_color = "#00CC77" if pnl >= 0 else "#FF3355"
    pnl_sign  = "+" if pnl >= 0 else ""
    pnl_str   = f"{pnl_sign}{pnl_pct:+.3f}% ({pnl_sign}${abs(pnl):.2f} USDT)"

    dir_color = "#00CC77" if direction in ("BUY", "LONG") else "#FF3355"

    # Analysis
    a_overall  = data.get("analysis_overall", "")
    a_setup    = float(data.get("analysis_setup",    0) or 0)
    a_risk     = float(data.get("analysis_risk",     0) or 0)
    a_exec     = float(data.get("analysis_execution",0) or 0)
    a_decision = float(data.get("analysis_decision", 0) or 0)
    a_rr       = e(str(data.get("analysis_rr", "—")))
    a_cls      = data.get("analysis_classification", "")
    a_emoji    = data.get("analysis_emoji", "")
    a_rc       = e(str(data.get("analysis_root_causes",    "") or ""))
    a_rec      = e(str(data.get("analysis_recommendation", "") or ""))

    analysis_html = ""
    if a_overall:
        badge_style = _classification_badge_style(a_cls)
        a_overall_f = float(a_overall)
        overall_color = _score_bar_color(a_overall_f)

        rc_row = ""
        if a_rc and a_rc != "None identified":
            rc_row = (f"<div class='row' style='margin-top:8px;'>"
                      f"<span class='lbl'>Root Causes</span>"
                      f"<span class='val amber' style='max-width:320px;text-align:right;'>"
                      f"{a_rc}</span></div>")
        rec_row = ""
        if a_rec and a_rec != "No specific recommendations.":
            rec_row = (f"<div style='margin-top:10px;padding:10px;background:#0A1020;"
                       f"border-left:3px solid #1E90FF;border-radius:4px;"
                       f"font-size:12px;line-height:1.5;color:#C8D0E0;'>"
                       f"<span style='color:#8899AA;'>💡 Recommendation: </span>{a_rec}"
                       f"</div>")

        analysis_html = f"""
        <div class="card">
          <div class="card-header">
            <span style="font-weight:700;font-size:13px;">🤖 AI Trade Quality Scorecard</span>
            <span class="badge" style="{badge_style};float:right;">
              {e(str(a_emoji))} {e(str(a_cls))} &nbsp; {e(str(a_overall))}/100
            </span>
          </div>
          <div class="card-body">
            <div style="text-align:center;margin-bottom:14px;">
              <div style="font-size:32px;font-weight:700;color:{overall_color};">{e(str(a_overall))}</div>
              <div style="font-size:11px;color:#8899AA;">Overall Score</div>
            </div>
            {_score_row_html("Setup Quality",     a_setup)}
            {_score_row_html("Risk Management",   a_risk)}
            {_score_row_html("Execution Quality", a_exec)}
            {_score_row_html("Decision Quality",  a_decision)}
            <div class="row" style="margin-top:8px;">
              <span class="lbl">R:R Ratio</span>
              <span class="val">{a_rr}</span>
            </div>
            {rc_row}
            {rec_row}
          </div>
        </div>"""

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>{_TRADE_HTML_CSS}</style></head>
<body><div class="wrap">

<div class="card">
  <div class="card-header" style="background:#0A1830;">
    <span style="font-size:16px;font-weight:700;color:#E8EBF0;">
      {"✅" if pnl >= 0 else "❌"} Trade Closed
    </span>
    <span style="float:right;color:{dir_color};font-weight:700;">{sym} {direction}</span>
  </div>
  <div class="card-body">
    <div class="row"><span class="lbl">Entry / Exit</span>
      <span class="val">{entry} → {exit_p}</span></div>
    <div class="row"><span class="lbl">P&amp;L</span>
      <span class="val" style="color:{pnl_color};font-size:15px;">{e(pnl_str)}</span></div>
    <div class="row"><span class="lbl">Size</span><span class="val">{size}</span></div>
    <div class="row"><span class="lbl">Strategy</span><span class="val">{strategy}</span></div>
    <div class="row"><span class="lbl">Exit Reason</span><span class="val">{reason}</span></div>
    <div class="row"><span class="lbl">Duration</span><span class="val">{duration}</span></div>
  </div>
</div>

{analysis_html}

<div class="footer">NexusTrader • {timestamp}</div>
</div></body></html>"""

    return html


# ── v1.2 Partial Exit Template ────────────────────────────────

def partial_exit(data: dict) -> dict[str, str]:
    """
    v1.2 notification: 33% partial close at +1R with SL moved to breakeven.
    """
    sym       = data.get("symbol", "???")
    direction = (data.get("direction", "LONG") or "LONG").upper()
    tf        = data.get("timeframe", "30m")
    regime    = data.get("regime", "—")
    entry     = _fmt_price(data.get("entry_price"))
    exit_p    = _fmt_price(data.get("exit_price"))
    pnl       = float(data.get("pnl_usdt", 0.0) or 0.0)
    pnl_sign  = "+" if pnl >= 0 else ""
    closed_sz = float(data.get("closed_size_usdt", 0.0) or 0.0)
    remaining = float(data.get("remaining_size_usdt", 0.0) or 0.0)
    close_pct = int(data.get("close_pct", 33))
    strategy  = data.get("strategy", "—")
    timestamp = _now_utc()

    subject = f"📉 NexusTrader | Partial Exit {sym} {direction} ({close_pct}%)"
    body = (
        f"{'='*46}\n"
        f"  NEXUS TRADER — PARTIAL EXIT\n"
        f"{'─'*46}\n"
        f"  Symbol    : {sym} {direction}\n"
        f"  Timeframe : {tf}  |  Regime: {regime}\n"
        f"  Entry     : {entry}  →  Exit: {exit_p}\n"
        f"  Closed    : {close_pct}% (${closed_sz:,.2f} USDT)\n"
        f"  P&L       : {pnl_sign}${abs(pnl):.2f} USDT\n"
        f"  Remaining : ${remaining:,.2f} USDT  (SL → breakeven)\n"
        f"  Strategy  : {strategy}\n"
        f"  Time      : {timestamp}\n"
        f"{'='*46}"
    )
    short = (
        f"📉 *Partial Exit* {sym} {direction} ({close_pct}%)\n"
        f"Exit: {exit_p} | P&L: {pnl_sign}${abs(pnl):.2f}\n"
        f"Remaining: ${remaining:,.2f} | SL→Breakeven ✅"
    )
    result: dict[str, str] = {"subject": subject, "body": body, "short": short}

    # Rich HTML body for email
    try:
        result["html_body"] = _build_partial_exit_html(data)
    except Exception:
        pass
    return result


def _build_partial_exit_html(data: dict) -> str:
    """Rich HTML body for partial_exit email (v1.2)."""
    import html as _html_mod
    e = _html_mod.escape
    sym       = e(data.get("symbol",    "???"))
    direction = (data.get("direction",  "LONG") or "LONG").upper()
    tf        = e(str(data.get("timeframe",  "30m")))
    regime    = e(str(data.get("regime",     "—")))
    entry     = e(_fmt_price(data.get("entry_price")))
    exit_p    = e(_fmt_price(data.get("exit_price")))
    pnl       = float(data.get("pnl_usdt", 0.0) or 0.0)
    pnl_sign  = "+" if pnl >= 0 else ""
    pnl_color = "#00CC77" if pnl >= 0 else "#FF3355"
    closed_sz = float(data.get("closed_size_usdt", 0.0) or 0.0)
    remaining = float(data.get("remaining_size_usdt", 0.0) or 0.0)
    close_pct = int(data.get("close_pct", 33))
    strategy  = e(str(data.get("strategy", "—")))
    timestamp = e(_now_utc())

    dir_color = "#00CC77" if direction in ("BUY", "LONG") else "#FF3355"

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>{_TRADE_HTML_CSS}</style></head>
<body><div class="wrap">

<div class="card">
  <div class="card-header" style="background:#0D1F0D;">
    <span style="font-size:16px;font-weight:700;color:#E8EBF0;">📉 Partial Exit ({close_pct}%)</span>
    <span style="float:right;color:{dir_color};font-weight:700;">{sym} {e(direction)}</span>
  </div>
  <div class="card-body">
    <div class="row"><span class="lbl">Entry → Exit</span>
      <span class="val">{entry} → {exit_p}</span></div>
    <div class="row"><span class="lbl">Realized P&amp;L</span>
      <span class="val" style="color:{pnl_color};font-size:15px;">{e(pnl_sign)}${abs(pnl):.2f} USDT</span></div>
    <div class="row"><span class="lbl">Closed Portion</span>
      <span class="val">{close_pct}% (${closed_sz:,.2f} USDT)</span></div>
    <div class="row"><span class="lbl">Remaining Size</span>
      <span class="val">${remaining:,.2f} USDT</span></div>
    <div class="row"><span class="lbl">Stop Loss</span>
      <span class="val green">✅ Moved to Breakeven</span></div>
    <div class="row"><span class="lbl">Strategy</span><span class="val">{strategy}</span></div>
    <div class="row"><span class="lbl">Timeframe</span><span class="val">{tf}</span></div>
    <div class="row"><span class="lbl">Regime</span><span class="val">{regime}</span></div>
  </div>
</div>

<div class="card">
  <div class="card-body" style="font-size:12px;color:#8899AA;line-height:1.6;">
    <strong style="color:#C8D0E0;">v1.2 Exit Logic:</strong> 33% of position closed at +1R target.
    Stop-loss has been moved to breakeven on the remaining 67%.
    The trade is now risk-free — worst case is breakeven on the remainder.
  </div>
</div>

<div class="footer">NexusTrader v1.2 • {timestamp}</div>
</div></body></html>"""
    return html


# ══════════════════════════════════════════════════════════════
# HTML builders for remaining templates (Session 38)
# All templates now produce a rich html_body key for email channels.
# Uses the same _TRADE_HTML_CSS base + shared _build_email_html() helper.
# ══════════════════════════════════════════════════════════════

def _esc(v) -> str:
    """HTML-escape any value to string."""
    return _html_mod.escape(str(v)) if v is not None else ""


def _build_email_html(
    title: str,
    subtitle: str,
    header_color: str,
    rows: "list[tuple[str, str, str]]",
    timestamp: str,
    alert_box: "Optional[str]" = None,
    extra_sections: "list[tuple[str, list[tuple[str,str,str]]]]" = None,
    badge_html: str = "",
) -> str:
    """
    Generic professional HTML email builder.

    Parameters
    ----------
    title         : Main heading (e.g. "STOP-LOSS HIT")
    subtitle      : Sub-heading (e.g. "BTCUSDT · LONG")
    header_color  : Left-border accent colour (hex)
    rows          : list of (label, value, value_color_hex) — use "" for default colour
    timestamp     : UTC timestamp string
    alert_box     : Optional red/amber highlighted message at bottom of first card
    extra_sections: Optional list of (section_title, rows) additional cards
    badge_html    : Optional HTML injected next to the title (right-aligned badge)
    """
    def _row(label: str, value: str, color: str = "") -> str:
        vc = f"color:{color};" if color else ""
        return (
            f'<tr>'
            f'<td style="padding:5px 0;font-size:13px;color:#8899AA;'
            f'width:38%;vertical-align:top">{_esc(label)}</td>'
            f'<td style="padding:5px 0 5px 8px;font-size:13px;font-weight:600;'
            f'color:#E8EBF0;{vc}vertical-align:top">{value}</td>'
            f'</tr>'
        )

    rows_html = "".join(_row(l, v, c) for l, v, c in rows)

    alert_html = ""
    if alert_box:
        alert_html = (
            f'<div style="margin-top:12px;padding:10px 14px;background:#1A0A0A;'
            f'border-left:3px solid #FF3355;border-radius:4px;font-size:12px;'
            f'color:#FCA5A5;line-height:1.6">{_esc(alert_box)}</div>'
        )

    extra_html = ""
    if extra_sections:
        for sec_title, sec_rows in extra_sections:
            sec_rows_html = "".join(_row(l, v, c) for l, v, c in sec_rows)
            extra_html += f"""
<div class="card" style="margin-top:10px">
  <div class="card-header">
    <span style="font-weight:700;font-size:13px;color:#C8D0E0">{_esc(sec_title)}</span>
  </div>
  <div class="card-body">
    <table width="100%" cellpadding="0" cellspacing="0">{sec_rows_html}</table>
  </div>
</div>"""

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>{_TRADE_HTML_CSS}</style></head>
<body><div class="wrap">

<!-- Header card -->
<div class="card" style="border-left:3px solid {header_color}">
  <div class="card-header" style="border-bottom:1px solid #1A2332">
    <table width="100%" cellpadding="0" cellspacing="0"><tr>
      <td style="vertical-align:middle">
        <span style="font-weight:700;font-size:14px;color:{header_color}">{_esc(title)}</span>
        <span style="margin-left:8px;font-size:12px;color:#8899AA">{_esc(subtitle)}</span>
      </td>
      <td align="right" style="vertical-align:middle">{badge_html}</td>
    </tr></table>
  </div>
  <div class="card-body">
    <table width="100%" cellpadding="0" cellspacing="0">
{rows_html}
    </table>
{alert_html}
  </div>
</div>

{extra_html}

<div class="footer">NexusTrader &nbsp;·&nbsp; {_esc(timestamp)}</div>
</div></body></html>"""


# ── trade_stopped HTML ─────────────────────────────────────────

def _build_trade_stopped_html(data: dict) -> str:
    sym       = _esc(data.get("symbol", "???"))
    direction = (data.get("direction", "long") or "long").upper()
    entry     = _esc(_fmt_price(data.get("entry_price")))
    stop      = _esc(_fmt_price(data.get("stop_price")))
    loss      = data.get("loss", 0.0)
    loss_pct  = data.get("loss_pct", 0.0)
    loss_str  = f"−{abs(float(loss_pct or 0)):.2f}% &nbsp;({_fmt_price(abs(float(loss or 0)), 2)} USDT)"
    ts        = _esc(_now_utc())
    dir_color = "#00CC77" if direction in ("BUY", "LONG") else "#FF3355"
    dir_badge = (
        f'<span style="background:{"#064E3B" if direction in ("BUY","LONG") else "#7F1D1D"};'
        f'color:{dir_color};font-size:10px;font-weight:700;padding:2px 7px;'
        f'border-radius:3px;letter-spacing:1px">{direction}</span>'
    )
    return _build_email_html(
        title="STOP-LOSS HIT",
        subtitle=f"{sym} · {direction}",
        header_color="#FF3355",
        rows=[
            ("Asset",       f"{sym} &nbsp;{dir_badge}", ""),
            ("Entry Price", entry,                       "#E8EBF0"),
            ("Stop Triggered", stop,                     "#FF3355"),
            ("Loss",        loss_str,                    "#FF3355"),
            ("Time",        ts,                          "#8899AA"),
        ],
        timestamp=ts,
        alert_box="Position closed by stop-loss. Review trade parameters before next entry.",
    )


# ── trade_rejected HTML ────────────────────────────────────────

def _build_trade_rejected_html(data: dict) -> str:
    sym      = _esc(data.get("symbol", "???"))
    strategy = _esc(str(data.get("strategy", "—")))
    reason   = _esc(str(data.get("reason", "risk gate")))
    conf     = data.get("confidence", 0.0)
    regime   = _esc(str(data.get("regime", "—")))
    ts       = _esc(_now_utc())
    conf_pct = f"{float(conf or 0):.0%}"
    conf_color = "#00CC77" if float(conf or 0) >= 0.60 else ("#FFB300" if float(conf or 0) >= 0.40 else "#FF3355")
    return _build_email_html(
        title="SIGNAL REJECTED",
        subtitle=sym,
        header_color="#FFB300",
        rows=[
            ("Asset",      sym,       ""),
            ("Strategy",   strategy,  "#E8EBF0"),
            ("Confidence", conf_pct,  conf_color),
            ("Regime",     regime,    "#93C5FD"),
            ("Reject Reason", reason, "#FCA5A5"),
            ("Time",       ts,        "#8899AA"),
        ],
        timestamp=ts,
    )


# ── trade_modified HTML ────────────────────────────────────────

def _build_trade_modified_html(data: dict) -> str:
    sym     = _esc(data.get("symbol", "???"))
    change  = _esc(str(data.get("change_description", "parameters updated")))
    old_sl  = _esc(_fmt_price(data.get("old_stop_loss")))
    new_sl  = _esc(_fmt_price(data.get("new_stop_loss")))
    old_tp  = _esc(_fmt_price(data.get("old_take_profit")))
    new_tp  = _esc(_fmt_price(data.get("new_take_profit")))
    ts      = _esc(_now_utc())
    arrow   = ' <span style="color:#4A6A8A">→</span> '
    return _build_email_html(
        title="TRADE MODIFIED",
        subtitle=sym,
        header_color="#1E90FF",
        rows=[
            ("Asset",      sym,                              ""),
            ("Change",     change,                           "#E8EBF0"),
            ("Stop Loss",  f"{old_sl}{arrow}{new_sl}",       "#FFB300"),
            ("Take Profit",f"{old_tp}{arrow}{new_tp}",       "#00CC77"),
            ("Time",       ts,                               "#8899AA"),
        ],
        timestamp=ts,
    )


# ── strategy_signal HTML ───────────────────────────────────────

def _build_strategy_signal_html(data: dict) -> str:
    sym      = _esc(data.get("symbol", "???"))
    direction= (data.get("direction", "long") or "long").upper()
    strategy = _esc(str(data.get("strategy", "—")))
    conf     = data.get("confidence", 0.0)
    regime   = _esc(str(data.get("regime", "—")))
    entry    = _esc(_fmt_price(data.get("entry_price")))
    sl       = _esc(_fmt_price(data.get("stop_loss")))
    tp       = _esc(_fmt_price(data.get("take_profit")))
    signals  = data.get("contributing_signals", [])
    sigs_str = _esc(", ".join(signals[:5])) if signals else "—"
    ts       = _esc(_now_utc())
    conf_pct = f"{float(conf or 0):.0%}"
    dir_color = "#00CC77" if direction in ("BUY", "LONG") else "#FF3355"
    dir_badge = (
        f'<span style="background:{"#064E3B" if direction in ("BUY","LONG") else "#7F1D1D"};'
        f'color:{dir_color};font-size:10px;font-weight:700;padding:2px 7px;'
        f'border-radius:3px;letter-spacing:1px">{direction}</span>'
    )
    return _build_email_html(
        title="STRATEGY SIGNAL",
        subtitle=f"{sym} · {direction}",
        header_color="#1E90FF",
        rows=[
            ("Asset",       f"{sym} &nbsp;{dir_badge}", ""),
            ("Strategy",    strategy,                    "#E8EBF0"),
            ("Confidence",  conf_pct,                    "#00CC77"),
            ("Regime",      regime,                      "#93C5FD"),
        ],
        timestamp=ts,
        extra_sections=[
            ("Entry Parameters", [
                ("Entry Price",  entry,   "#E8EBF0"),
                ("Stop Loss",    sl,      "#FF3355"),
                ("Take Profit",  tp,      "#00CC77"),
                ("Signals",      sigs_str,"#8899AA"),
            ]),
        ],
    )


# ── risk_warning HTML ──────────────────────────────────────────

def _build_risk_warning_html(data: dict) -> str:
    warning_type = _esc(str(data.get("warning_type", "Risk Warning")))
    level        = str(data.get("level", "high")).lower()
    message      = _esc(str(data.get("message", "Risk threshold exceeded")))
    current_val  = _esc(str(data.get("current_value", "—")))
    threshold    = _esc(str(data.get("threshold", "—")))
    ts           = _esc(_now_utc())
    level_colors = {
        "low":      ("#00CC77", "#0A3320"),
        "medium":   ("#FFB300", "#2A1A00"),
        "high":     ("#FF3355", "#3A0A14"),
        "critical": ("#FF0033", "#500010"),
    }
    lc, lbg = level_colors.get(level, ("#FF3355", "#3A0A14"))
    level_badge = (
        f'<span style="background:{lbg};color:{lc};font-size:10px;font-weight:700;'
        f'padding:2px 8px;border-radius:3px;letter-spacing:1px">{level.upper()}</span>'
    )
    return _build_email_html(
        title="RISK WARNING",
        subtitle=warning_type,
        header_color=lc,
        rows=[
            ("Warning Type", warning_type,       ""),
            ("Level",        level_badge,        ""),
            ("Message",      message,            "#FCA5A5"),
            ("Current Value",current_val,        "#FFB300"),
            ("Threshold",    threshold,          "#8899AA"),
            ("Time",         ts,                "#8899AA"),
        ],
        timestamp=ts,
        alert_box=f"RISK ACTION REQUIRED — {message}",
    )


# ── market_condition HTML ──────────────────────────────────────

def _build_market_condition_html(data: dict) -> str:
    condition  = _esc(str(data.get("condition", "Market Alert")))
    regime     = _esc(str(data.get("regime", "—")))
    message    = _esc(str(data.get("message", "")))
    confidence = data.get("confidence", 0.0)
    ts         = _esc(_now_utc())
    conf_pct   = f"{float(confidence or 0):.0%}"
    return _build_email_html(
        title="MARKET CONDITION ALERT",
        subtitle=condition,
        header_color="#1E90FF",
        rows=[
            ("Condition",  condition,  "#E8EBF0"),
            ("Regime",     regime,     "#93C5FD"),
            ("Confidence", conf_pct,   "#FFB300"),
            ("Details",    message,    "#C8D0E0"),
            ("Time",       ts,        "#8899AA"),
        ],
        timestamp=ts,
    )


# ── system_error HTML ─────────────────────────────────────────

def _build_system_error_html(data: dict) -> str:
    component = _esc(str(data.get("component", "System")))
    error     = _esc(str(data.get("error", "Unknown error")))
    severity  = str(data.get("severity", "error")).upper()
    ts        = _esc(_now_utc())
    sev_color = "#FF0033" if severity == "CRITICAL" else "#FF3355"
    sev_badge = (
        f'<span style="background:#3A0A14;color:{sev_color};font-size:10px;'
        f'font-weight:700;padding:2px 8px;border-radius:3px;letter-spacing:1px">'
        f'{severity}</span>'
    )
    return _build_email_html(
        title="SYSTEM ERROR",
        subtitle=component,
        header_color="#FF3355",
        rows=[
            ("Component", component,  "#E8EBF0"),
            ("Severity",  sev_badge,  ""),
            ("Error",     error,      "#FCA5A5"),
            ("Time",      ts,        "#8899AA"),
        ],
        timestamp=ts,
        alert_box=f"SYSTEM ALERT: {component} reported an error. Immediate review required.",
    )


# ── system_alert HTML ─────────────────────────────────────────

def _build_system_alert_html(data: dict) -> str:
    title_str = _esc(str(data.get("title", "System Alert")))
    message   = _esc(str(data.get("message", "")))
    ts        = _esc(_now_utc())
    return _build_email_html(
        title="SYSTEM ALERT",
        subtitle=title_str,
        header_color="#1E90FF",
        rows=[
            ("Alert",   title_str, "#E8EBF0"),
            ("Details", message,   "#C8D0E0"),
            ("Time",    ts,       "#8899AA"),
        ],
        timestamp=ts,
    )


# ── emergency_stop HTML ───────────────────────────────────────

def _build_emergency_stop_html(data: dict) -> str:
    reason   = _esc(str(data.get("reason", "emergency stop triggered")))
    open_pos = _esc(str(data.get("open_positions", 0)))
    equity   = _esc(_fmt_price(data.get("equity"), 2))
    ts       = _esc(_now_utc())
    return _build_email_html(
        title="⚠ EMERGENCY STOP ACTIVATED",
        subtitle="ALL TRADING HALTED",
        header_color="#FF0033",
        rows=[
            ("Reason",          reason,   "#FCA5A5"),
            ("Positions Closed",open_pos, "#FF3355"),
            ("Equity",          f"{equity} USDT", "#E8EBF0"),
            ("Status",          "TRADING HALTED — Manual review required", "#FF3355"),
            ("Time",            ts,       "#8899AA"),
        ],
        timestamp=ts,
        alert_box="EMERGENCY STOP: All positions have been closed. Do not resume trading without reviewing the cause.",
    )


# ── crash_* HTML builders ─────────────────────────────────────

def _build_crash_html(
    tier: str,
    score: float,
    actions: str,
    advisory: str,
    header_color: str,
    ts: str,
) -> str:
    score_str = f"{score:.1f} / 10.0"
    # Score bar
    pct = min(100, int(score * 10))
    bar = (
        f'<div style="background:#141E2E;border-radius:3px;height:6px;margin-top:4px">'
        f'<div style="background:{header_color};width:{pct}%;height:6px;border-radius:3px"></div>'
        f'</div>'
    )
    return _build_email_html(
        title=f"CRASH DEFENSE — {tier}",
        subtitle=f"Score: {score_str}",
        header_color=header_color,
        rows=[
            ("Tier",         f'<span style="font-weight:700;color:{header_color}">{_esc(tier)}</span>', ""),
            ("Crash Score",  f"{score_str} {bar}", ""),
            ("Actions Taken",_esc(actions), "#FCA5A5"),
            ("Time",         _esc(ts), "#8899AA"),
        ],
        timestamp=_esc(ts),
        alert_box=advisory,
    )


def _build_crash_defensive_html(data: dict) -> str:
    return _build_crash_html(
        tier="DEFENSIVE",
        score=float(data.get("score", 0.0)),
        actions=str(data.get("actions", "New longs halted")),
        advisory="Defensive mode active. No new long entries permitted. Monitor closely.",
        header_color="#FFB300",
        ts=str(data.get("timestamp", _now_utc())),
    )


def _build_crash_high_alert_html(data: dict) -> str:
    return _build_crash_html(
        tier="HIGH ALERT",
        score=float(data.get("score", 0.0)),
        actions=str(data.get("actions", "50% of long positions closed")),
        advisory="High Alert: 50% of longs closed. Trailing stops activated on remaining positions.",
        header_color="#FF3355",
        ts=str(data.get("timestamp", _now_utc())),
    )


def _build_crash_emergency_html(data: dict) -> str:
    return _build_crash_html(
        tier="EMERGENCY",
        score=float(data.get("score", 0.0)),
        actions=str(data.get("actions", "All long positions closed")),
        advisory="EMERGENCY: All longs closed. System in READ-ONLY mode. No new trades until manual override.",
        header_color="#FF0033",
        ts=str(data.get("timestamp", _now_utc())),
    )


def _build_crash_systemic_html(data: dict) -> str:
    return _build_crash_html(
        tier="SYSTEMIC CRISIS",
        score=float(data.get("score", 0.0)),
        actions=str(data.get("actions", "All positions closed")),
        advisory="SYSTEMIC CRISIS: ALL positions closed. SAFE MODE active. Manual restart required before resuming.",
        header_color="#CC0033",
        ts=str(data.get("timestamp", _now_utc())),
    )


# ── daily_summary HTML ────────────────────────────────────────

def _build_daily_summary_html(data: dict) -> str:
    date     = _esc(str(data.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))))
    trades   = int(data.get("total_trades", 0))
    wins     = int(data.get("wins", 0))
    losses   = int(data.get("losses", 0))
    pnl      = float(data.get("daily_pnl", 0.0) or 0.0)
    pnl_pct  = float(data.get("daily_pnl_pct", 0.0) or 0.0)
    win_rate = float(data.get("win_rate", 0.0) or 0.0)
    equity   = _esc(_fmt_price(data.get("equity"), 2))
    regime   = _esc(str(data.get("current_regime", "—")))
    ts       = _esc(_now_utc())

    pnl_color = "#00CC77" if pnl >= 0 else "#FF3355"
    pnl_str   = f'<span style="color:{pnl_color};font-weight:700">{("+" if pnl>=0 else "")}{pnl_pct:.2f}% ({("+" if pnl>=0 else "")}{_fmt_price(pnl, 2)} USDT)</span>'
    wr_color  = "#00CC77" if win_rate >= 50 else "#FFB300"

    # Win rate bar
    wr_bar = (
        f'<div style="background:#141E2E;border-radius:3px;height:5px;margin-top:4px;max-width:200px">'
        f'<div style="background:{wr_color};width:{min(100,win_rate):.0f}%;height:5px;border-radius:3px"></div>'
        f'</div>'
    )

    return _build_email_html(
        title="DAILY SUMMARY",
        subtitle=date,
        header_color="#1E90FF",
        rows=[
            ("Date",        date,   "#E8EBF0"),
            ("Daily P&amp;L", pnl_str, ""),
            ("Equity",      f"{equity} USDT", "#E8EBF0"),
            ("Total Trades", str(trades), "#E8EBF0"),
            ("Wins / Losses",f'{wins}W &nbsp;/&nbsp; {losses}L', "#E8EBF0"),
            ("Win Rate",    f'{win_rate:.1f}% {wr_bar}', wr_color),
            ("Regime",      regime, "#93C5FD"),
            ("Time",        ts,    "#8899AA"),
        ],
        timestamp=ts,
    )


# ── Wire html_body into the remaining template functions ───────
# (the 13 templates that previously returned no html_body)

def _wrap_html(fn):
    """Decorator: call html builder named '_build_{fn.__name__}_html', attach result."""
    import functools
    builder_name = f"_build_{fn.__name__}_html"

    @functools.wraps(fn)
    def wrapper(data: dict) -> dict:
        result = fn(data)
        builder = globals().get(builder_name)
        if builder is not None:
            try:
                result["html_body"] = builder(data)
            except Exception:
                pass
        return result
    return wrapper


# Apply to all templates that had no html_body
trade_stopped    = _wrap_html(trade_stopped)
trade_rejected   = _wrap_html(trade_rejected)
trade_modified   = _wrap_html(trade_modified)
strategy_signal  = _wrap_html(strategy_signal)
risk_warning     = _wrap_html(risk_warning)
market_condition = _wrap_html(market_condition)
system_error     = _wrap_html(system_error)
system_alert     = _wrap_html(system_alert)
emergency_stop   = _wrap_html(emergency_stop)
crash_defensive  = _wrap_html(crash_defensive)
crash_high_alert = _wrap_html(crash_high_alert)
crash_emergency  = _wrap_html(crash_emergency)
crash_systemic   = _wrap_html(crash_systemic)
daily_summary    = _wrap_html(daily_summary)


# ── Template registry ─────────────────────────────────────────
TEMPLATES = {
    "trade_opened":     trade_opened,
    "trade_closed":     trade_closed,
    "partial_exit":     partial_exit,
    "trade_stopped":    trade_stopped,
    "trade_rejected":   trade_rejected,
    "trade_modified":   trade_modified,
    "strategy_signal":  strategy_signal,
    "risk_warning":     risk_warning,
    "market_condition": market_condition,
    "system_error":     system_error,
    "system_alert":     system_alert,
    "emergency_stop":   emergency_stop,
    "crash_defensive":  crash_defensive,
    "crash_high_alert": crash_high_alert,
    "crash_emergency":  crash_emergency,
    "crash_systemic":   crash_systemic,
    "daily_summary":    daily_summary,
    "health_check":     health_check,
}


def render(template_name: str, data: dict) -> dict[str, str]:
    """
    Render a notification template.
    Returns dict with 'subject', 'body', 'short' keys.
    Falls back to a generic template if name unknown.
    """
    fn = TEMPLATES.get(template_name)
    if fn:
        return fn(data)
    # Generic fallback
    title = data.get("title", template_name.replace("_", " ").title())
    message = data.get("message", str(data))
    return {
        "subject": f"NEXUS TRADER — {title}",
        "body": f"{'='*42}\n  NEXUS TRADER\n  {title}\n{'─'*42}\n  {message}\n  Time: {_now_utc()}\n{'='*42}",
        "short": f"*{title}*\n{message}",
    }
