# ============================================================
# NEXUS TRADER — WhatsApp Notification Channel
#
# Sends notifications via Twilio WhatsApp Business API.
# Requires:
#   - Twilio Account SID  (key_vault "notifications.twilio_sid")
#   - Twilio Auth Token   (key_vault "notifications.twilio_token")
#   - Twilio WhatsApp Number  (settings notifications.whatsapp_from)
#   - Recipient WhatsApp Number (settings notifications.whatsapp_to)
#
# Numbers must be in E.164 format prefixed with "whatsapp:"
# e.g. whatsapp:+14155238886 (Twilio sandbox)
#       whatsapp:+15551234567 (recipient)
#
# Rate limiting: 1 message per 3 seconds (Twilio rate limit guard)
# ============================================================
from __future__ import annotations

import logging
import time
import threading
from typing import Optional

logger = logging.getLogger(__name__)

_MIN_INTERVAL_S = 3.0   # Twilio WA rate-limit buffer


class WhatsAppChannel:
    """
    Sends messages via Twilio WhatsApp API.
    Thread-safe; includes rate limiting.
    """

    def __init__(self, config: dict):
        """
        config keys:
            account_sid  : str  (Twilio Account SID)
            auth_token   : str  (Twilio Auth Token)
            from_number  : str  (e.g. "whatsapp:+14155238886")
            to_number    : str  (e.g. "whatsapp:+15551234567")
            enabled      : bool (default True)
        """
        self._sid   = config.get("account_sid", "")
        self._token = config.get("auth_token", "")
        self._from  = config.get("from_number", "")
        self._to    = config.get("to_number", "")
        self._enabled = config.get("enabled", True)
        self._lock    = threading.RLock()
        self._last_sent: float = 0.0

    @property
    def name(self) -> str:
        return "whatsapp"

    @property
    def is_configured(self) -> bool:
        return bool(self._sid and self._token and self._from and self._to)

    def send(self, message: str, subject: Optional[str] = None) -> bool:
        """
        Send a WhatsApp message.
        message: the body text (use 'short' template for WhatsApp).
        Returns True on success, False on failure.
        """
        if not self._enabled:
            logger.debug("WhatsAppChannel: disabled, skipping")
            return False

        if not self.is_configured:
            logger.warning("WhatsAppChannel: not configured (missing credentials)")
            return False

        # Rate limit
        with self._lock:
            elapsed = time.time() - self._last_sent
            if elapsed < _MIN_INTERVAL_S:
                time.sleep(_MIN_INTERVAL_S - elapsed)
            self._last_sent = time.time()

        try:
            from twilio.rest import Client
            client = Client(self._sid, self._token)
            msg = client.messages.create(
                from_=self._from,
                to=self._to,
                body=message,
            )
            logger.info("WhatsAppChannel: sent SID=%s", msg.sid)
            return True

        except ImportError:
            logger.error(
                "WhatsAppChannel: twilio package not installed. "
                "Run: pip install twilio"
            )
            return False

        except Exception as exc:
            logger.error("WhatsAppChannel: send failed — %s", exc)
            return False

    def test(self) -> bool:
        """Send a test message to verify configuration."""
        from core.notifications.notification_templates import render
        content = render("system_alert", {
            "title": "WhatsApp Channel Test",
            "message": "NexusTrader WhatsApp notifications are configured correctly.",
        })
        return self.send(content["short"])
