# ============================================================
# NEXUS TRADER — Gemini (Google) Notification Channel
#
# Sends trade/system notifications to the user's Google/Gmail
# account.  Works in two complementary modes:
#
#   1. Gmail delivery (always)
#      SMTP to smtp.gmail.com:587 — pre-configured for Google
#      accounts.  Requires an App Password (Settings ▶
#      Notifications ▶ Gemini).
#
#   2. Gemini AI enrichment (optional, when api_key set)
#      Passes the raw alert through Gemini Flash to produce a
#      concise, AI-annotated summary that is appended to the
#      email body.  Uses the same API key as the AI tab.
#
# Setup
# -----
#   1. Go to myaccount.google.com → Security → 2-Step Verification
#      → App passwords.
#   2. Create an App Password named "NexusTrader".
#   3. Paste it into Settings ▶ Notifications ▶ Gemini channel.
#   4. Optionally enable AI enrichment — uses the Gemini API
#      key you already entered on the AI tab.
# ============================================================
from __future__ import annotations

import json
import logging
import smtplib
import threading
import urllib.error
import urllib.request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

logger = logging.getLogger(__name__)

_GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/"
    "models/{model}:generateContent?key={api_key}"
)
_GEMINI_DEFAULT_MODEL = "gemini-2.0-flash"
_AI_ENRICH_TIMEOUT    = 10   # seconds
_SMTP_TIMEOUT         = 15   # seconds


class GeminiChannel:
    """
    Gmail-based notification channel with optional Gemini AI enrichment.

    channel.name == "gemini"
    Thread-safe.
    """

    def __init__(self, config: dict):
        """
        config keys
        -----------
        smtp_host          : str   default "smtp.gmail.com"
        smtp_port          : int   default 587
        username           : str   your Gmail address
        password           : str   Google App Password (not main password)
        from_address       : str   defaults to username
        to_address         : str   recipient (defaults to username — self-notify)
        use_tls            : bool  default True
        ai_enrich          : bool  default False
        gemini_api_key     : str   Gemini API key (optional, for AI enrichment)
        gemini_model       : str   default "gemini-2.0-flash"
        enabled            : bool  default True
        """
        self._host      = config.get("smtp_host", "smtp.gmail.com")
        self._port      = int(config.get("smtp_port", 587))
        self._user      = config.get("username", "")
        self._pass      = config.get("password", "")
        self._from      = config.get("from_address", "") or self._user
        to_raw          = config.get("to_address", "") or self._user
        self._to        = [to_raw] if isinstance(to_raw, str) else list(to_raw)
        self._use_tls   = config.get("use_tls", True)
        self._ai_enrich = config.get("ai_enrich", False)
        self._api_key   = config.get("gemini_api_key", "")
        self._model     = config.get("gemini_model", _GEMINI_DEFAULT_MODEL)
        self._enabled   = config.get("enabled", True)
        self._lock      = threading.RLock()

    # ── Properties ────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "gemini"

    @property
    def is_configured(self) -> bool:
        return bool(self._host and self._user and self._pass and self._to)

    # ── Send ──────────────────────────────────────────────────

    def send(
        self,
        message: str,
        subject: Optional[str] = None,
        html_body: Optional[str] = None,
    ) -> bool:
        """
        Send a notification email via Gmail.
        Optionally enriches body with Gemini AI analysis.
        html_body : rich HTML body from template; when supplied the channel
                    uses it directly (AI enrichment appended in a separate card
                    below if available).
        Returns True on success.
        """
        if not self._enabled:
            logger.debug("GeminiChannel: disabled, skipping")
            return False

        if not self.is_configured:
            logger.warning(
                "GeminiChannel: not configured (missing Gmail credentials)"
            )
            return False

        subject = subject or "NexusTrader — Gemini Notification"
        body    = message

        # Optional Gemini AI enrichment (plain-text only)
        enrichment_html = ""
        if self._ai_enrich and self._api_key:
            try:
                enriched = self._ai_enrich_text(message)
                if enriched:
                    body = message + "\n\n── Gemini Analysis ───────────────────\n" + enriched
                    import html as _h
                    enrichment_html = (
                        "<div style='margin-top:14px;border:1px solid #1A3050;"
                        "border-left:3px solid #4285F4;border-radius:6px;"
                        "padding:14px 16px;background:#0A1628;font-size:12px;"
                        "color:#93C5FD;font-family:Arial,sans-serif'>"
                        "<div style='font-size:10px;letter-spacing:1px;color:#4285F4;"
                        "font-weight:700;margin-bottom:8px'>GEMINI AI ANALYSIS</div>"
                        f"<pre style='margin:0;white-space:pre-wrap;color:#C8D0E0'>"
                        f"{_h.escape(enriched)}</pre></div>"
                    )
            except Exception as exc:
                logger.debug("GeminiChannel: AI enrichment skipped — %s", exc)

        with self._lock:
            try:
                msg = MIMEMultipart("alternative")
                msg["Subject"] = subject
                msg["From"]    = self._from
                msg["To"]      = ", ".join(self._to)

                # Plain text
                text_part = MIMEText(body, "plain", "utf-8")

                # HTML — use rich template when provided; insert Gemini card
                # before the closing </body> tag if enrichment is available.
                if html_body:
                    if enrichment_html and "</body>" in html_body:
                        _html = html_body.replace("</body>", enrichment_html + "</body>", 1)
                    else:
                        _html = html_body
                else:
                    _html = (
                        "<html><body style='font-family:monospace;background:#0A0E1A;"
                        "color:#C8D0E0;padding:20px'>"
                        "<div style='border-left:3px solid #4285F4;padding-left:12px;"
                        "margin-bottom:16px'>"
                        "<span style='color:#4285F4;font-size:11px;font-weight:bold;"
                        "letter-spacing:1px'>NEXUSTRADER  ·  GEMINI CHANNEL</span>"
                        "</div>"
                        f"<pre style='color:#C8D0E0;white-space:pre-wrap'>{body}</pre>"
                        + enrichment_html +
                        "</body></html>"
                )
                html_part = MIMEText(_html, "html", "utf-8")

                msg.attach(text_part)
                msg.attach(html_part)

                if self._use_tls:
                    server = smtplib.SMTP(self._host, self._port,
                                          timeout=_SMTP_TIMEOUT)
                    server.ehlo()
                    server.starttls()
                    server.ehlo()
                else:
                    server = smtplib.SMTP_SSL(self._host, self._port,
                                              timeout=_SMTP_TIMEOUT)

                server.login(self._user, self._pass)
                server.sendmail(self._from, self._to, msg.as_string())
                server.quit()

                logger.info(
                    "GeminiChannel: notification sent to %s | subject=%s",
                    self._to, subject,
                )
                return True

            except smtplib.SMTPAuthenticationError:
                logger.error(
                    "GeminiChannel: Gmail authentication failed — "
                    "check credentials and ensure 2FA + App Password are set up"
                )
                return False

            except smtplib.SMTPException as exc:
                logger.error("GeminiChannel: SMTP error — %s", exc)
                return False

            except Exception as exc:
                logger.error("GeminiChannel: send failed — %s", exc)
                return False

    def test(self) -> bool:
        """Send a test notification to verify Gmail configuration."""
        from core.notifications.notification_templates import render
        try:
            content = render("system_alert", {
                "title":   "Gemini Channel Test",
                "message": (
                    "NexusTrader Gemini notifications are configured correctly.\n"
                    "You will receive trade alerts at this address."
                ),
            })
            return self.send(content["body"], subject=content["subject"])
        except Exception as exc:
            logger.error("GeminiChannel: test failed — %s", exc)
            # Fallback to plain text if template unavailable
            return self.send(
                "✅ NexusTrader — Gemini channel test successful.",
                subject="NexusTrader — Test Notification",
            )

    # ── Gemini AI enrichment ───────────────────────────────────

    def _ai_enrich_text(self, message: str) -> Optional[str]:
        """
        Call Gemini API to produce a brief AI analysis of the alert.
        Returns the analysis string, or None on failure.
        """
        url = _GEMINI_API_URL.format(
            model=self._model, api_key=self._api_key
        )
        prompt = (
            "You are a concise crypto trading assistant. "
            "In 2-3 sentences, briefly analyze the following trading alert "
            "and highlight the most important action point:\n\n"
            f"{message}"
        )
        payload = json.dumps({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "maxOutputTokens": 150,
                "temperature": 0.3,
            },
        }).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_AI_ENRICH_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())

        candidates = data.get("candidates", [])
        if not candidates:
            return None
        parts = candidates[0].get("content", {}).get("parts", [])
        if not parts:
            return None
        return parts[0].get("text", "").strip() or None
