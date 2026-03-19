# ============================================================
# NEXUS TRADER — Telegram Notification Channel
#
# Sends notifications via Telegram Bot API (free, no extra lib).
# Requires:
#   - Bot Token   (key_vault "notifications.telegram_token")
#   - Chat ID     (settings notifications.telegram_chat_id)
#
# Uses urllib (stdlib only) — no python-telegram-bot dependency.
# Markdown V2 formatting for rich messages.
#
# Rate limiting: 30 messages per second (Telegram Bot API limit)
# NexusTrader uses 1 per second to be safe.
# ============================================================
from __future__ import annotations

import json
import logging
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
_MIN_INTERVAL_S = 1.0   # 1 message / second safety guard
_TIMEOUT_S = 10


class TelegramChannel:
    """
    Sends messages via Telegram Bot API using stdlib urllib.
    Thread-safe; supports Markdown formatting.
    """

    def __init__(self, config: dict):
        """
        config keys:
            bot_token : str   (from @BotFather)
            chat_id   : str   (can be numeric or @channel_name)
            enabled   : bool  (default True)
            parse_mode: str   (default "Markdown")
        """
        self._token     = config.get("bot_token", "")
        self._chat_id   = config.get("chat_id", "")
        self._enabled   = config.get("enabled", True)
        self._parse_mode= config.get("parse_mode", "Markdown")
        self._lock      = threading.RLock()
        self._last_sent: float = 0.0

    @property
    def name(self) -> str:
        return "telegram"

    @property
    def is_configured(self) -> bool:
        return bool(self._token and self._chat_id)

    def send(self, message: str, subject: Optional[str] = None) -> bool:
        """
        Send a Telegram message.
        message: use 'short' template (supports Markdown).
        Returns True on success, False on failure.
        """
        if not self._enabled:
            logger.debug("TelegramChannel: disabled, skipping")
            return False

        if not self.is_configured:
            logger.warning("TelegramChannel: not configured (missing token or chat_id)")
            return False

        # Rate limit
        with self._lock:
            elapsed = time.time() - self._last_sent
            if elapsed < _MIN_INTERVAL_S:
                time.sleep(_MIN_INTERVAL_S - elapsed)
            self._last_sent = time.time()

        try:
            url = _TELEGRAM_API.format(token=self._token, method="sendMessage")
            payload = json.dumps({
                "chat_id":    self._chat_id,
                "text":       message,
                "parse_mode": self._parse_mode,
                "disable_web_page_preview": True,
            }).encode("utf-8")

            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )

            with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
                resp_data = json.loads(resp.read().decode())
                if resp_data.get("ok"):
                    msg_id = resp_data.get("result", {}).get("message_id", "?")
                    logger.info("TelegramChannel: sent message_id=%s", msg_id)
                    return True
                else:
                    logger.error(
                        "TelegramChannel: API error — %s",
                        resp_data.get("description", "unknown"),
                    )
                    return False

        except urllib.error.HTTPError as exc:
            logger.error("TelegramChannel: HTTP error %d — %s", exc.code, exc.reason)
            return False

        except Exception as exc:
            logger.error("TelegramChannel: send failed — %s", exc)
            return False

    def test(self) -> bool:
        """Send a test message to verify configuration."""
        return self.send(
            "✅ *NexusTrader* — Telegram notifications configured correctly.\n"
            f"Bot connected | Chat ID: `{self._chat_id}`"
        )
