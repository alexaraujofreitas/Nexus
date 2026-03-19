# ============================================================
# NEXUS TRADER — SMS Notification Channel
#
# Sends notifications via Twilio SMS API.
# Shares the same Twilio credentials as WhatsApp channel
# (same Account SID + Auth Token), different "from" number.
#
# Requires:
#   - Twilio Account SID  (key_vault "notifications.twilio_sid")
#   - Twilio Auth Token   (key_vault "notifications.twilio_token")
#   - Twilio SMS From Number  (settings notifications.sms_from)
#   - Recipient Phone Number  (settings notifications.sms_to)
#
# SMS messages are limited to 160 chars per segment.
# NexusTrader uses the 'short' template which is designed to
# fit within ~2 SMS segments.
# ============================================================
from __future__ import annotations

import logging
import time
import threading
from typing import Optional

logger = logging.getLogger(__name__)

_MIN_INTERVAL_S = 1.0   # Twilio SMS rate limit buffer


class SMSChannel:
    """
    Sends SMS messages via Twilio REST API.
    Thread-safe; uses same Twilio account as WhatsApp.
    """

    def __init__(self, config: dict):
        """
        config keys:
            account_sid  : str  (Twilio Account SID)
            auth_token   : str  (Twilio Auth Token)
            from_number  : str  (e.g. "+14155238886" — plain E.164, NOT whatsapp:)
            to_number    : str  (e.g. "+15551234567")
            enabled      : bool (default True)
        """
        self._sid     = config.get("account_sid", "")
        self._token   = config.get("auth_token", "")
        self._from    = config.get("from_number", "")
        self._to      = config.get("to_number", "")
        self._enabled = config.get("enabled", True)
        self._lock    = threading.RLock()
        self._last_sent: float = 0.0

    @property
    def name(self) -> str:
        return "sms"

    @property
    def is_configured(self) -> bool:
        return bool(self._sid and self._token and self._from and self._to)

    def send(self, message: str, subject: Optional[str] = None) -> bool:
        """
        Send an SMS.
        message: use 'short' template — emoji stripped to ASCII for SMS compat.
        Returns True on success, False on failure.
        """
        if not self._enabled:
            logger.debug("SMSChannel: disabled, skipping")
            return False

        if not self.is_configured:
            logger.warning("SMSChannel: not configured (missing Twilio credentials)")
            return False

        # Rate limit
        with self._lock:
            elapsed = time.time() - self._last_sent
            if elapsed < _MIN_INTERVAL_S:
                time.sleep(_MIN_INTERVAL_S - elapsed)
            self._last_sent = time.time()

        # Strip Markdown bold/italic for plain SMS
        sms_body = (
            message
            .replace("*", "")
            .replace("`", "")
            .replace("_", " ")
        )
        # Truncate to 320 chars (2 SMS segments max)
        if len(sms_body) > 320:
            sms_body = sms_body[:317] + "..."

        try:
            from twilio.rest import Client
            client = Client(self._sid, self._token)
            msg = client.messages.create(
                from_=self._from,
                to=self._to,
                body=sms_body,
            )
            logger.info("SMSChannel: sent SID=%s", msg.sid)
            return True

        except ImportError:
            logger.error(
                "SMSChannel: twilio package not installed. "
                "Run: pip install twilio"
            )
            return False

        except Exception as exc:
            logger.error("SMSChannel: send failed — %s", exc)
            return False

    def test(self) -> bool:
        """Send a test SMS to verify configuration."""
        return self.send(
            "NexusTrader: SMS notifications configured correctly. "
            f"Recipient: {self._to}"
        )
