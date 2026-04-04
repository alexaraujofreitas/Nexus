# ============================================================
# NEXUS TRADER — Health Check Notification Tests
#
# Covers: _build_health_html(), health_check() template,
#         EmailChannel.send() / GeminiChannel.send() html_body param,
#         _collect_health_data() open/closed detail blocks.
# ============================================================
"""
Run with:
    pytest tests/unit/test_health_check_notification.py -v
"""
from __future__ import annotations

import html as _html_mod
from datetime import datetime, timezone, timedelta
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_open_trade(
    symbol: str = "BTCUSDT",
    side: str = "buy",
    entry_price: float = 98_000.0,
    current_price: float = 99_500.0,
    size_usdt: float = 5_000.0,
    unrealized_pnl: float = 1.53,   # percent
    stop_loss: float = 96_000.0,
    take_profit: float = 103_000.0,
    score: float = 0.72,
    regime: str = "bull_trend",
    models_fired: Optional[list] = None,
    timeframe: str = "1h",
    opened_at: Optional[str] = None,
) -> dict:
    return {
        "symbol":         symbol,
        "side":           side,
        "entry_price":    entry_price,
        "current_price":  current_price,
        "quantity":       size_usdt / entry_price,
        "stop_loss":      stop_loss,
        "take_profit":    take_profit,
        "size_usdt":      size_usdt,
        "entry_size_usdt": size_usdt,
        "unrealized_pnl": unrealized_pnl,
        "score":          score,
        "rationale":      "Momentum breakout on rising volume",
        "regime":         regime,
        "models_fired":   models_fired or ["TrendModel", "MomentumBreakout"],
        "timeframe":      timeframe,
        "opened_at":      opened_at or datetime.now(timezone.utc).isoformat(),
        "trailing_stop_pct": 2.0,
        "bars_held":      4,
        "highest_price":  99_800.0,
        "lowest_price":   97_800.0,
    }


def _make_closed_trade(
    symbol: str = "ETHUSDT",
    side: str = "buy",
    entry_price: float = 3_200.0,
    exit_price: float = 3_380.0,
    pnl_usdt: float = 225.0,
    pnl_pct: float = 5.63,
    exit_reason: str = "take_profit",
    score: float = 0.68,
    regime: str = "bull_trend",
    models_fired: Optional[list] = None,
    timeframe: str = "1h",
    duration_s: int = 8_100,   # 2h 15m
    won: bool = True,
) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "symbol":         symbol,
        "side":           side,
        "entry_price":    entry_price,
        "exit_price":     exit_price,
        "stop_loss":      entry_price * 0.98,
        "take_profit":    exit_price,
        "size_usdt":      abs(pnl_usdt / (pnl_pct / 100)) if pnl_pct else 4_000.0,
        "entry_size_usdt": abs(pnl_usdt / (pnl_pct / 100)) if pnl_pct else 4_000.0,
        "exit_size_usdt": abs(pnl_usdt / (pnl_pct / 100)) if pnl_pct else 4_000.0,
        "pnl_pct":        pnl_pct if won else -abs(pnl_pct),
        "pnl_usdt":       pnl_usdt if won else -abs(pnl_usdt),
        "exit_reason":    exit_reason,
        "score":          score,
        "rationale":      "Entry on EMA crossover",
        "regime":         regime,
        "models_fired":   models_fired or ["MomentumBreakout"],
        "timeframe":      timeframe,
        "duration_s":     duration_s,
        "opened_at":      (now - timedelta(seconds=duration_s)).isoformat(),
        "closed_at":      now.isoformat(),
        "risk_amount_usdt": 100.0,
        "expected_rr":    2.5,
        "symbol_weight":  1.0,
        "adjusted_score": score,
    }


def _make_data(open_trades=None, closed_trades=None) -> dict:
    return {
        "scanner_status":    "Running",
        "last_scan_ago":     "5m ago",
        "exchange_status":   "Connected (Bybit)",
        "feed_status":       "Active",
        "ai_status":         "Online (Ollama/deepseek-r1:14b)",
        "portfolio_value":   102_450.37,
        "available_cash":    88_200.10,
        "today_pnl":         725.50,
        "today_pnl_pct":     0.71,
        "win_rate":          62.5,
        "total_trades":      16,
        "open_positions":    len(open_trades or []),
        "open_trades_detail":  open_trades  or [],
        "closed_trades_detail": closed_trades or [],
    }


# ── Template: health_check() ──────────────────────────────────────────────────

class TestHealthCheckTemplate:
    """Tests for notification_templates.health_check()."""

    def _render(self, data: dict) -> dict:
        from core.notifications.notification_templates import health_check
        return health_check(data)

    # ── Return structure ───────────────────────────────────────────────────────

    def test_returns_required_keys(self):
        result = self._render(_make_data())
        assert "subject"  in result
        assert "body"     in result
        assert "short"    in result

    def test_returns_html_body_key_when_no_error(self):
        result = self._render(_make_data())
        assert "html_body" in result
        assert result["html_body"].strip().startswith("<!DOCTYPE html>")

    def test_html_body_none_not_in_result_on_crash(self):
        """If _build_health_html raises, html_body should not appear (fails gracefully)."""
        from core.notifications import notification_templates as tpl_mod
        original = tpl_mod._build_health_html
        try:
            tpl_mod._build_health_html = lambda d: (_ for _ in ()).throw(RuntimeError("boom"))
            result = self._render(_make_data())
            # Should still have subject/body/short but not html_body
            assert "subject" in result
            assert "html_body" not in result
        finally:
            tpl_mod._build_health_html = original

    # ── No trades ─────────────────────────────────────────────────────────────

    def test_no_trades_empty_state(self):
        result = self._render(_make_data())
        assert "No open positions" in result["html_body"]
        assert "No closed trades yet" in result["html_body"]

    def test_no_trades_plain_body_no_trade_section(self):
        result = self._render(_make_data())
        assert "OPEN POSITIONS" not in result["body"]
        assert "RECENT CLOSED TRADES" not in result["body"]

    # ── Open trades only ──────────────────────────────────────────────────────

    def test_open_trade_appears_in_html(self):
        open_t = [_make_open_trade(symbol="BTCUSDT")]
        result = self._render(_make_data(open_trades=open_t))
        html   = result["html_body"]
        assert "BTCUSDT"       in html
        assert "LONG"          in html
        assert "OPEN POSITIONS (1)" in html

    def test_open_trade_appears_in_plain_body(self):
        open_t = [_make_open_trade(symbol="SOLUSDT", side="sell", unrealized_pnl=-0.45)]
        result = self._render(_make_data(open_trades=open_t))
        assert "SOLUSDT" in result["body"]
        assert "SHORT"   in result["body"]
        assert "OPEN POSITIONS (1)" in result["body"]

    def test_multiple_open_trades_all_appear(self):
        open_t = [
            _make_open_trade(symbol="BTCUSDT"),
            _make_open_trade(symbol="ETHUSDT", side="sell"),
            _make_open_trade(symbol="SOLUSDT"),
        ]
        result = self._render(_make_data(open_trades=open_t))
        html   = result["html_body"]
        assert "BTCUSDT" in html
        assert "ETHUSDT" in html
        assert "SOLUSDT" in html
        assert "OPEN POSITIONS (3)" in html

    def test_open_trade_pnl_positive_green(self):
        open_t = [_make_open_trade(unrealized_pnl=2.5)]
        result = self._render(_make_data(open_trades=open_t))
        # Green color for positive P&L
        assert "#10B981" in result["html_body"]

    def test_open_trade_pnl_negative_red(self):
        open_t = [_make_open_trade(unrealized_pnl=-1.2)]
        result = self._render(_make_data(open_trades=open_t))
        assert "#EF4444" in result["html_body"]

    def test_open_trade_fields_present_in_html(self):
        open_t = [_make_open_trade(
            symbol="BNBUSDT",
            regime="ranging",
            models_fired=["TrendModel"],
        )]
        html = self._render(_make_data(open_trades=open_t))["html_body"]
        assert "BNBUSDT" in html
        assert "ranging" in html
        assert "TrendModel" in html

    def test_no_closed_trades_section_shows_empty_state(self):
        open_t = [_make_open_trade()]
        html = self._render(_make_data(open_trades=open_t))["html_body"]
        assert "No closed trades yet" in html

    # ── Closed trades only ────────────────────────────────────────────────────

    def test_closed_trade_appears_in_html(self):
        closed = [_make_closed_trade(symbol="ETHUSDT", won=True)]
        result = self._render(_make_data(closed_trades=closed))
        html   = result["html_body"]
        assert "ETHUSDT"    in html
        assert "WIN"        in html
        assert "take profit" in html  # exit_reason spaces rendered

    def test_closed_trade_loss_appears_correctly(self):
        closed = [_make_closed_trade(symbol="SOLUSDT", won=False,
                                     pnl_usdt=-48.0, pnl_pct=-1.2,
                                     exit_reason="stop_loss")]
        html = self._render(_make_data(closed_trades=closed))["html_body"]
        assert "SOLUSDT"     in html
        assert "LOSS"        in html
        assert "stop loss"   in html
        assert "#EF4444"     in html

    def test_closed_trades_plain_body_summary(self):
        closed = [_make_closed_trade(symbol="XRPUSDT", won=True, pnl_usdt=55.0)]
        result = self._render(_make_data(closed_trades=closed))
        assert "RECENT CLOSED TRADES" in result["body"]
        assert "XRPUSDT" in result["body"]

    def test_no_open_positions_section_shows_empty_state(self):
        closed = [_make_closed_trade()]
        html = self._render(_make_data(closed_trades=closed))["html_body"]
        assert "No open positions" in html

    # ── Both open and closed ──────────────────────────────────────────────────

    def test_both_open_and_closed_appear(self):
        open_t  = [_make_open_trade(symbol="BTCUSDT")]
        closed  = [_make_closed_trade(symbol="ETHUSDT")]
        result  = self._render(_make_data(open_trades=open_t, closed_trades=closed))
        html    = result["html_body"]
        assert "BTCUSDT" in html
        assert "ETHUSDT" in html
        assert "No open positions"    not in html
        assert "No closed trades yet" not in html

    # ── Edge cases: None / missing fields ─────────────────────────────────────

    def test_open_trade_missing_fields_no_crash(self):
        """Minimal dict with only symbol should not raise."""
        open_t = [{"symbol": "BTCUSDT"}]
        result = self._render(_make_data(open_trades=open_t))
        assert "BTCUSDT" in result["html_body"]

    def test_closed_trade_missing_fields_no_crash(self):
        closed = [{"symbol": "ETHUSDT", "pnl_usdt": None, "pnl_pct": None}]
        result = self._render(_make_data(closed_trades=closed))
        assert "ETHUSDT" in result["html_body"]

    def test_none_models_fired_renders_dash(self):
        open_t = [_make_open_trade(models_fired=None)]
        html   = self._render(_make_data(open_trades=open_t))["html_body"]
        # Should not crash and should render a dash
        assert "html" in html  # basic sanity

    def test_empty_models_fired_list(self):
        open_t = [_make_open_trade(models_fired=[])]
        # Should not crash
        result = self._render(_make_data(open_trades=open_t))
        assert "html_body" in result

    def test_negative_portfolio_renders(self):
        data = _make_data()
        data["portfolio_value"] = -500.0
        data["today_pnl"]       = -500.0
        data["today_pnl_pct"]   = -0.50
        result = self._render(data)
        assert "html_body" in result
        assert "#EF4444" in result["html_body"]  # red for negative P&L

    def test_zero_trades_win_rate_shows_dash(self):
        data = _make_data()
        data["total_trades"] = 0
        data["win_rate"]     = 0.0
        result = self._render(data)
        assert "n/a" in result["body"]  # plain text
        # HTML should show "—" for win rate
        assert result["html_body"]  # doesn't crash

    # ── HTML safety ───────────────────────────────────────────────────────────

    def test_html_special_chars_in_symbol_escaped(self):
        """A symbol with < > & should be HTML-escaped."""
        open_t = [_make_open_trade(symbol="<script>")]
        html   = self._render(_make_data(open_trades=open_t))["html_body"]
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_html_special_chars_in_regime_escaped(self):
        open_t = [_make_open_trade(regime='bull & bear > uncertain')]
        html   = self._render(_make_data(open_trades=open_t))["html_body"]
        assert "bull & bear > uncertain" not in html

    # ── Portfolio metrics ─────────────────────────────────────────────────────

    def test_portfolio_value_in_header(self):
        data = _make_data()
        data["portfolio_value"] = 102_450.37
        html = self._render(data)["html_body"]
        assert "102,450.37" in html

    def test_system_status_all_present(self):
        html = self._render(_make_data())["html_body"]
        assert "IDSS Scanner"    in html
        assert "Exchange"        in html
        assert "Data Feed"       in html
        assert "AI Provider"     in html

    def test_subject_contains_portfolio(self):
        data = _make_data()
        data["portfolio_value"] = 50_000.0
        subject = self._render(data)["subject"]
        assert "50,000.00" in subject

    # ── Short message ─────────────────────────────────────────────────────────

    def test_short_message_contains_key_fields(self):
        result = self._render(_make_data())
        short  = result["short"]
        assert "Health Check" in short
        assert "Scanner"      in short
        assert "Exchange"     in short


# ── _build_health_html() unit tests ───────────────────────────────────────────

class TestBuildHealthHtml:
    """Direct tests for _build_health_html()."""

    def _build(self, data: dict) -> str:
        from core.notifications.notification_templates import _build_health_html
        return _build_health_html(data)

    def test_returns_valid_html_string(self):
        html = self._build(_make_data())
        assert isinstance(html, str)
        assert "<!DOCTYPE html>" in html
        assert "</html>" in html

    def test_open_section_count_label(self):
        open_t = [_make_open_trade(), _make_open_trade(symbol="ETHUSDT")]
        html   = self._build(_make_data(open_trades=open_t))
        assert "OPEN POSITIONS (2)" in html

    def test_closed_section_count_label(self):
        closed = [_make_closed_trade(symbol=f"SYM{i}USDT") for i in range(5)]
        data   = _make_data(closed_trades=closed)
        data["total_trades"] = 20  # more than shown
        html   = self._build(data)
        assert "5 of 20" in html

    def test_most_recent_closed_shown_first(self):
        """Cards are rendered most-recent-first (reversed list)."""
        now = datetime.now(timezone.utc)
        older  = _make_closed_trade(symbol="OLDUSDT")
        older["closed_at"] = (now - timedelta(hours=2)).isoformat()
        newer  = _make_closed_trade(symbol="NEWUSDT")
        newer["closed_at"] = now.isoformat()
        html = self._build(_make_data(closed_trades=[older, newer]))
        # NEWUSDT should appear before OLDUSDT in the HTML string
        assert html.index("NEWUSDT") < html.index("OLDUSDT")

    def test_long_badge_green(self):
        open_t = [_make_open_trade(side="buy")]
        html   = self._build(_make_data(open_trades=open_t))
        assert "#064E3B" in html   # LONG background
        assert "#34D399" in html   # LONG text

    def test_short_badge_red(self):
        open_t = [_make_open_trade(side="sell")]
        html   = self._build(_make_data(open_trades=open_t))
        assert "#7F1D1D" in html   # SHORT background
        assert "#FCA5A5" in html   # SHORT text

    def test_footer_present(self):
        html = self._build(_make_data())
        assert "Demo Mode" in html


# ── EmailChannel.send() html_body param ──────────────────────────────────────

class TestEmailChannelHtmlBody:
    """Verify EmailChannel.send() uses html_body when provided."""

    def _make_channel(self):
        from core.notifications.channels.email_channel import EmailChannel
        cfg = {
            "smtp_host":    "smtp.test.com",
            "smtp_port":    587,
            "username":     "test@test.com",
            "password":     "pw",
            "from_address": "test@test.com",
            "to_addresses": ["rcpt@test.com"],
            "enabled":      True,
        }
        return EmailChannel(cfg)

    def test_accepts_html_body_parameter(self):
        """send() should not raise TypeError when html_body is passed."""
        import inspect
        from core.notifications.channels.email_channel import EmailChannel
        sig = inspect.signature(EmailChannel.send)
        assert "html_body" in sig.parameters

    def test_uses_rich_html_when_provided(self):
        """When html_body is set, MIMEText gets that content, not the <pre> fallback."""
        ch = self._make_channel()
        rich_html = "<html><body>RICH</body></html>"
        captured_html: list[str] = []

        # Patch MIMEText at the module-local name so the already-imported reference
        # inside email_channel.py is intercepted.
        import email.mime.text as _orig_mod
        _OrigMIMEText = _orig_mod.MIMEText

        def _capture(content, subtype="plain", charset="us-ascii"):
            if subtype == "html":
                captured_html.append(content)
            return _OrigMIMEText(content, subtype, charset)

        with patch("core.notifications.channels.email_channel.MIMEText",
                   side_effect=_capture), \
             patch("smtplib.SMTP") as mock_smtp:
            mock_smtp.return_value = MagicMock()
            ch.send("plain text body", subject="Test", html_body=rich_html)

        assert captured_html, "No HTML MIME part was created"
        assert rich_html in captured_html
        assert "<pre" not in captured_html[0]   # fallback was NOT used

    def test_falls_back_to_pre_when_no_html_body(self):
        """When html_body is None, the <pre> fallback is used."""
        ch = self._make_channel()
        captured_html: list[str] = []

        import email.mime.text as _orig_mod
        _OrigMIMEText = _orig_mod.MIMEText

        def _capture(content, subtype="plain", charset="us-ascii"):
            if subtype == "html":
                captured_html.append(content)
            return _OrigMIMEText(content, subtype, charset)

        with patch("core.notifications.channels.email_channel.MIMEText",
                   side_effect=_capture), \
             patch("smtplib.SMTP") as mock_smtp:
            mock_smtp.return_value = MagicMock()
            ch.send("plain text body", subject="Test", html_body=None)

        assert captured_html, "No HTML MIME part was created"
        assert "<pre" in captured_html[0]   # fallback was used


# ── GeminiChannel.send() html_body param ─────────────────────────────────────

class TestGeminiChannelHtmlBody:
    """Verify GeminiChannel.send() uses html_body when provided."""

    def test_accepts_html_body_parameter(self):
        import inspect
        from core.notifications.channels.gemini_channel import GeminiChannel
        sig = inspect.signature(GeminiChannel.send)
        assert "html_body" in sig.parameters

    def test_uses_rich_html_when_provided(self):
        from core.notifications.channels.gemini_channel import GeminiChannel
        cfg = {
            "username":  "user@gmail.com",
            "password":  "apppass",
            "to_address":"user@gmail.com",
            "enabled":   True,
        }
        ch = GeminiChannel(cfg)
        rich_html = "<html><body>GEMINI_RICH</body></html>"
        captured_html: list[str] = []

        import email.mime.text as _orig_mod
        _OrigMIMEText = _orig_mod.MIMEText

        def _capture(content, subtype="plain", charset="us-ascii"):
            if subtype == "html":
                captured_html.append(content)
            return _OrigMIMEText(content, subtype, charset)

        with patch("core.notifications.channels.gemini_channel.MIMEText",
                   side_effect=_capture), \
             patch("smtplib.SMTP") as mock_smtp:
            mock_smtp.return_value = MagicMock()
            ch.send("plain text", subject="Test", html_body=rich_html)

        assert captured_html, "No HTML MIME part was created"
        assert rich_html in captured_html
        assert "<pre" not in captured_html[0]

    def test_falls_back_to_pre_when_no_html_body(self):
        from core.notifications.channels.gemini_channel import GeminiChannel
        cfg = {
            "username":  "user@gmail.com",
            "password":  "apppass",
            "to_address":"user@gmail.com",
            "enabled":   True,
        }
        ch = GeminiChannel(cfg)
        captured_html: list[str] = []

        import email.mime.text as _orig_mod
        _OrigMIMEText = _orig_mod.MIMEText

        def _capture(content, subtype="plain", charset="us-ascii"):
            if subtype == "html":
                captured_html.append(content)
            return _OrigMIMEText(content, subtype, charset)

        with patch("core.notifications.channels.gemini_channel.MIMEText",
                   side_effect=_capture), \
             patch("smtplib.SMTP") as mock_smtp:
            mock_smtp.return_value = MagicMock()
            ch.send("plain text", subject="Test")

        assert captured_html, "No HTML MIME part was created"
        assert "<pre" in captured_html[0]
        assert "NEXUSTRADER" in captured_html[0]   # Gemini brand header


# ── _send_on_channel html_body routing ────────────────────────────────────────

class TestSendOnChannelHtmlRouting:
    """Verify _send_on_channel passes html_body to email-capable channels."""

    def _make_record(self):
        from core.notifications.notification_manager import _NotifRecord
        return _NotifRecord(template="health_check", dedup_key="")

    def test_html_body_passed_to_email_channel(self):
        """email channel.send() should receive html_body when content has it."""
        import importlib
        import core.notifications.notification_manager as nm_mod
        importlib.reload(nm_mod)  # fresh import

        mock_ch = MagicMock()
        mock_ch.name = "email"
        mock_ch.send.return_value = True
        mock_ch._check_twilio_rate_limit = MagicMock(return_value=True)  # not used for email

        content = {
            "body":     "plain text",
            "subject":  "Test",
            "html_body": "<html>RICH</html>",
        }
        record = self._make_record()

        # Build a minimal NotificationManager with empty internals
        nm = object.__new__(nm_mod.NotificationManager)
        nm._lock          = __import__("threading").RLock()
        nm._delivery_stats= {"total_sent": 0, "total_failed": 0, "total_retried": 0}
        nm._history       = []
        nm._retry_queue   = __import__("queue").Queue()
        nm._check_twilio_rate_limit = lambda: True

        nm._send_on_channel(mock_ch, content, record)

        mock_ch.send.assert_called_once_with(
            "plain text",
            subject="Test",
            html_body="<html>RICH</html>",
        )

    def test_no_html_body_for_whatsapp_channel(self):
        """whatsapp channel.send() should NOT receive html_body (uses short message)."""
        import core.notifications.notification_manager as nm_mod

        mock_ch = MagicMock()
        mock_ch.name = "whatsapp"
        mock_ch.send.return_value = True

        content = {
            "short":     "short msg",
            "body":      "plain text",
            "subject":   "Test",
            "html_body": "<html>RICH</html>",
        }
        record = self._make_record()

        nm = object.__new__(nm_mod.NotificationManager)
        nm._lock           = __import__("threading").RLock()
        nm._delivery_stats = {"total_sent": 0, "total_failed": 0, "total_retried": 0}
        nm._history        = []
        nm._retry_queue    = __import__("queue").Queue()
        # _check_twilio_rate_limit is needed for whatsapp — return True (allowed)
        nm._check_twilio_rate_limit = lambda: True

        nm._send_on_channel(mock_ch, content, record)

        # html_body should NOT be passed to whatsapp send
        call_kwargs = mock_ch.send.call_args.kwargs
        assert "html_body" not in call_kwargs or call_kwargs.get("html_body") is None
