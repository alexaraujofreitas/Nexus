# ============================================================
# NEXUS TRADER — Email Notification Channel
#
# Sends notifications via SMTP (stdlib smtplib — no extra dep).
# Supports Gmail, Outlook, custom SMTP.
#
# Requires:
#   - SMTP host/port  (settings)
#   - Username/password  (key_vault "notifications.email_password")
#   - From/to addresses  (settings)
#
# Uses TLS/STARTTLS. For Gmail use App Passwords (not your
# main account password — Google requires 2FA + App Password).
# ============================================================
from __future__ import annotations

import logging
import smtplib
import threading
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

logger = logging.getLogger(__name__)


class EmailChannel:
    """
    Sends HTML+plain-text emails via SMTP.
    Thread-safe.
    """

    def __init__(self, config: dict):
        """
        config keys:
            smtp_host     : str  (e.g. "smtp.gmail.com")
            smtp_port     : int  (e.g. 587 for STARTTLS)
            username      : str  (SMTP login username)
            password      : str  (SMTP login password / app password)
            from_address  : str  (e.g. "nexustrader@gmail.com")
            to_addresses  : list[str] | str  (recipients)
            use_tls       : bool (default True — STARTTLS)
            enabled       : bool (default True)
        """
        self._host    = config.get("smtp_host", "")
        self._port    = int(config.get("smtp_port", 587))
        self._user    = config.get("username", "")
        self._pass    = config.get("password", "")
        self._from    = config.get("from_address", "")
        to_raw        = config.get("to_addresses", [])
        self._to      = [to_raw] if isinstance(to_raw, str) else list(to_raw)
        self._use_tls = config.get("use_tls", True)
        self._enabled = config.get("enabled", True)
        self._lock    = threading.RLock()

    @property
    def name(self) -> str:
        return "email"

    @property
    def is_configured(self) -> bool:
        return bool(self._host and self._user and self._pass and self._from and self._to)

    def send(
        self,
        message: str,
        subject: Optional[str] = None,
        html_body: Optional[str] = None,
    ) -> bool:
        """
        Send an email.
        message   : plain-text body (the 'body' template key)
        subject   : email subject line (the 'subject' template key)
        html_body : rich HTML body from template; when supplied the channel
                    uses it directly instead of the <pre> fallback.
        Returns True on success, False on failure.
        """
        if not self._enabled:
            logger.debug("EmailChannel: disabled, skipping")
            return False

        if not self.is_configured:
            logger.warning("EmailChannel: not configured (missing SMTP credentials)")
            return False

        subject = subject or "NexusTrader Notification"

        with self._lock:
            try:
                msg = MIMEMultipart("alternative")
                msg["Subject"] = subject
                msg["From"]    = self._from
                msg["To"]      = ", ".join(self._to)

                # Plain text part (always included for non-HTML clients)
                text_part = MIMEText(message, "plain", "utf-8")

                # HTML part — use rich template HTML when provided,
                # otherwise fall back to a styled <pre> wrapper.
                if html_body:
                    _html = html_body
                else:
                    _html = (
                        "<html><body style='font-family:monospace;background:#0A0E1A;"
                        "color:#C8D0E0;padding:20px'>"
                        f"<pre style='color:#C8D0E0'>{message}</pre>"
                        "</body></html>"
                    )
                html_part = MIMEText(_html, "html", "utf-8")

                # Attach both parts — email client picks best
                msg.attach(text_part)
                msg.attach(html_part)

                if self._use_tls:
                    server = smtplib.SMTP(self._host, self._port, timeout=15)
                    server.ehlo()
                    server.starttls()
                    server.ehlo()
                else:
                    server = smtplib.SMTP_SSL(self._host, self._port, timeout=15)

                server.login(self._user, self._pass)
                server.sendmail(self._from, self._to, msg.as_string())
                server.quit()

                logger.info(
                    "EmailChannel: sent to %d recipients | subject=%s",
                    len(self._to), subject,
                )
                return True

            except smtplib.SMTPAuthenticationError:
                logger.error("EmailChannel: authentication failed — check credentials")
                return False

            except smtplib.SMTPException as exc:
                logger.error("EmailChannel: SMTP error — %s", exc)
                return False

            except Exception as exc:
                logger.error("EmailChannel: send failed — %s", exc)
                return False

    def test(self) -> bool:
        """Send a test email to verify configuration."""
        from core.notifications.notification_templates import render
        content = render("system_alert", {
            "title": "Email Channel Test",
            "message": "NexusTrader email notifications are configured correctly.",
        })
        return self.send(content["body"], subject=content["subject"])
