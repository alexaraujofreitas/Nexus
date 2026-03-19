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
    Returns {'subject': ..., 'body': ..., 'short': ...}
    data keys: symbol, direction, entry_price, size, stop_loss, take_profit,
               strategy, confidence, rationale, timeframe, regime
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
        f"{'─'*42}\n"
        f"  Time:        {_now_utc()}\n"
        f"{'='*42}"
    )

    short = (
        f"🚀 *{sym}* {_dir(direction)} @ {entry}\n"
        f"SL: {sl} | TP: {tp}\n"
        f"Strategy: {strategy} | Conf: {conf:.0%}"
    )

    return {"subject": subject, "body": body, "short": short}


def trade_closed(data: dict) -> dict[str, str]:
    """
    data keys: symbol, direction, entry_price, exit_price, pnl, pnl_pct,
               size, strategy, close_reason, duration
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

    subject = f"{pnl_sign} Trade Closed — {sym} | {_fmt_pct(pnl_pct)}"

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
        f"{'─'*42}\n"
        f"  Time:        {_now_utc()}\n"
        f"{'='*42}"
    )

    short = (
        f"{pnl_sign} *{sym}* closed @ {exit_p}\n"
        f"Entry: {entry} | P&L: {pnl_str}\n"
        f"Reason: {reason}"
    )

    return {"subject": subject, "body": body, "short": short}


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


def health_check(data: dict) -> dict[str, str]:
    """
    4-hour system health check notification.
    data keys: scanner_status, last_scan_ago, exchange_status, feed_status,
               ai_status, portfolio_value, available_cash, today_pnl,
               today_pnl_pct, win_rate, total_trades, open_positions, timestamp
    """
    scanner      = data.get("scanner_status",  "Unknown")
    last_scan    = data.get("last_scan_ago",    "Unknown")
    exchange     = data.get("exchange_status", "Unknown")
    feed         = data.get("feed_status",     "Unknown")
    ai           = data.get("ai_status",       "Unknown")
    portfolio    = _fmt_price(data.get("portfolio_value"), 2)
    cash         = _fmt_price(data.get("available_cash"), 2)
    t_pnl        = data.get("today_pnl", 0.0)
    t_pnl_pct    = data.get("today_pnl_pct", 0.0)
    win_rate     = data.get("win_rate", 0.0)
    trades       = data.get("total_trades", 0)
    open_pos     = data.get("open_positions", 0)

    def _status_icon(s: str) -> str:
        s_lower = s.lower()
        if any(w in s_lower for w in ("running", "active", "connected", "ok", "online")):
            return "✅"
        if any(w in s_lower for w in ("error", "fail", "down", "inactive")):
            return "❌"
        return "⚠️"

    pnl_sign = "✅" if float(t_pnl or 0) >= 0 else "❌"
    pnl_str  = f"{pnl_sign} {_fmt_pct(t_pnl_pct)} ({'+' if float(t_pnl or 0) >= 0 else ''}{_fmt_price(t_pnl, 2)} USDT)"

    subject = f"💊 Health Check — Portfolio: {portfolio} USDT | Today P&L: {_fmt_pct(t_pnl_pct)}"

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
        f"  Portfolio:     {portfolio} USDT\n"
        f"  Available:     {cash} USDT\n"
        f"  Open Positions:{open_pos}\n"
        f"{'─'*42}\n"
        f"  PERFORMANCE\n"
        f"{'─'*42}\n"
        f"  Today P&L:     {pnl_str}\n"
        f"  Win Rate:      {win_rate:.1f}%\n"
        f"  Total Trades:  {trades}\n"
        f"{'='*42}"
    )

    short = (
        f"💊 *Health Check* — {_now_utc()}\n"
        f"{_status_icon(scanner)} Scanner: {scanner} (last scan: {last_scan}) | "
        f"{_status_icon(exchange)} Exchange: {exchange} | "
        f"{_status_icon(feed)} Feed: {feed}\n"
        f"Portfolio: {portfolio} USDT | Cash: {cash} USDT\n"
        f"Today P&L: {pnl_str} | WR: {win_rate:.1f}% | Trades: {trades} | Open: {open_pos}"
    )

    return {"subject": subject, "body": body, "short": short}


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


# ── Template registry ─────────────────────────────────────────
TEMPLATES = {
    "trade_opened":     trade_opened,
    "trade_closed":     trade_closed,
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
