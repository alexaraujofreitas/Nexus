# ============================================================
# NEXUS TRADER — Telegram Sentiment Agent
#
# Monitors Telegram sentiment for crypto signals via:
#   1. Telegram API (if TELEGRAM_BOT_TOKEN configured)
#   2. Telemetrio public stats API (free fallback)
#   3. t.me preview pages for public channels (web scrape)
#   4. Graceful degradation if all methods unavailable
#
# Key channels monitored:
#   • @whale_alert_io      → whale activity (deferred to WhaleAgent)
#   • @cryptowhale         → whale signals
#   • @crypto_pump         → pump signals
#   • @bitcoin_signal      → trading signals
#
# Publishes: Topics.TELEGRAM_SIGNAL
# Poll interval: 1200s (20 minutes) — slower cadence, more stable
# ============================================================
from __future__ import annotations

import logging
import threading
import urllib.request
import urllib.error
import json as _json
import re
from typing import Any, Optional
from datetime import datetime, timezone

from core.agents.base_agent import BaseAgent
from core.event_bus import Topics
from core.ai.model_registry import get_model_registry

logger = logging.getLogger(__name__)

_POLL_SECONDS = 1200  # 20 minutes

# Telegram channels to monitor
_CHANNELS = {
    "whale_alert_io": "whale_alert",
    "cryptowhale": "whale_alert",
    "crypto_pump": "pump_signal",
    "bitcoin_signal": "trading_signal",
    "AltcoinSherpa": "analysis",
    "BinanceNews": "exchange_news",
}


class TelegramSentimentAgent(BaseAgent):
    """
    Monitors Telegram channels for cryptocurrency market signals.

    Data sources:
      1. Telegram API (if bot token configured)
      2. Telemetrio public stats API (free fallback)
      3. t.me preview pages for public channels (web scrape)
      4. Graceful degradation with stale signals if all fail

    This agent respects privacy and only monitors public channels.
    Does not parse private groups or require user credentials.
    """

    def __init__(self, parent=None):
        super().__init__("telegram", parent)
        self._lock = threading.RLock()
        self._telegram_token: Optional[str] = None
        self._last_message_cache: dict[str, dict] = {}
        self._scorer = None
        self._load_telegram_token()

    def _load_telegram_token(self) -> None:
        """Load Telegram bot token from vault if available."""
        try:
            from core.security.key_vault import key_vault
            token = key_vault.load("telegram_bot_token")
            if token:
                self._telegram_token = token
                logger.info("TelegramAgent: Telegram API token loaded")
        except Exception:
            logger.debug("TelegramAgent: Telegram API token not available")

    @property
    def event_topic(self) -> str:
        return Topics.TELEGRAM_SIGNAL

    @property
    def poll_interval_seconds(self) -> int:
        return _POLL_SECONDS

    def on_settings_changed(self) -> None:
        """Reload token when settings change."""
        self._load_telegram_token()

    def fetch(self) -> dict:
        """Fetch Telegram channel activity from available sources."""
        raw: dict[str, Any] = {}

        # Try Telegram API first
        if self._telegram_token:
            try:
                api_data = self._fetch_telegram_api()
                if api_data:
                    raw["telegram_api"] = api_data
            except Exception as exc:
                logger.debug("TelegramAgent: Telegram API fetch failed — %s", exc)

        # Try Telemetrio (free stats)
        if not raw:
            try:
                telemetrio_data = self._fetch_telemetrio()
                if telemetrio_data:
                    raw["telemetrio"] = telemetrio_data
            except Exception as exc:
                logger.debug("TelegramAgent: Telemetrio fetch failed — %s", exc)

        # Try t.me preview pages
        if not raw:
            try:
                preview_data = self._fetch_tme_previews()
                if preview_data:
                    raw["tme_previews"] = preview_data
            except Exception as exc:
                logger.debug("TelegramAgent: t.me preview fetch failed — %s", exc)

        return raw

    def process(self, raw: dict) -> dict:
        """Convert raw Telegram data into normalized signal."""
        if not raw:
            return {
                "signal": 0.0,
                "confidence": 0.0,
                "has_data": False,
                "sentiment_label": "neutral",
                "active_channels": 0,
                "message_count_estimate": 0,
                "channel_signals": {},
                "alerts": [],
            }

        messages: list[dict] = []
        active_channels = set()
        channel_signals: dict[str, dict] = {}

        # Extract messages from all sources
        if "telegram_api" in raw:
            msg_data = raw["telegram_api"]
            messages.extend(msg_data.get("messages", []))
            active_channels.update(msg_data.get("channels", set()))
            channel_signals.update(msg_data.get("channel_signals", {}))

        if "telemetrio" in raw:
            msg_data = raw["telemetrio"]
            messages.extend(msg_data.get("messages", []))
            active_channels.update(msg_data.get("channels", set()))

        if "tme_previews" in raw:
            msg_data = raw["tme_previews"]
            messages.extend(msg_data.get("messages", []))
            active_channels.update(msg_data.get("channels", set()))

        if not messages:
            return {
                "signal": 0.0,
                "confidence": 0.0,
                "has_data": False,
                "sentiment_label": "neutral",
                "active_channels": len(active_channels),
                "message_count_estimate": 0,
                "channel_signals": channel_signals,
                "alerts": [],
            }

        # Score messages for sentiment
        signals_and_confs: list[tuple[float, float, dict]] = []
        alerts: list[dict] = []

        for msg in messages:
            text = msg.get("text", "")
            channel = msg.get("channel", "")

            # Score sentiment
            sig, conf = self._score_text(text)

            signals_and_confs.append((sig, conf, msg))

            # Detect pump/buy signals
            if self._is_pump_signal(text):
                alerts.append(
                    {
                        "type": "pump_signal",
                        "channel": channel,
                        "text": text[:200],
                        "signal": round(sig, 3),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                )

            # Detect whale activity (defer to WhaleAgent, just flag)
            if "whale" in channel.lower() and sig != 0:
                alerts.append(
                    {
                        "type": "whale_activity",
                        "channel": channel,
                        "text": text[:200],
                        "signal": round(sig, 3),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                )

        # Compute aggregate
        if not signals_and_confs:
            agg_sig = 0.0
            agg_conf = 0.0
        else:
            total_conf = sum(c for _, c, _ in signals_and_confs)
            agg_sig = (
                sum(s * c for s, c, _ in signals_and_confs) / total_conf
                if total_conf > 0 else 0.0
            )
            agg_conf = total_conf / len(signals_and_confs)

        agg_sig = max(-1.0, min(1.0, agg_sig))

        # Sentiment label
        sentiment_label = (
            "extremely_bullish" if agg_sig > 0.60 else
            "bullish" if agg_sig > 0.25 else
            "slightly_bullish" if agg_sig > 0.10 else
            "extremely_bearish" if agg_sig < -0.60 else
            "bearish" if agg_sig < -0.25 else
            "slightly_bearish" if agg_sig < -0.10 else
            "neutral"
        )

        # Cache for internal use
        with self._lock:
            for msg in messages[:10]:
                ch = msg.get("channel", "unknown")
                self._last_message_cache[ch] = msg

        logger.info(
            "TelegramAgent: signal=%+.3f | conf=%.2f | label=%s | channels=%d | alerts=%d",
            agg_sig, agg_conf, sentiment_label, len(active_channels), len(alerts),
        )

        return {
            "signal": round(agg_sig, 4),
            "confidence": round(agg_conf, 4),
            "has_data": True,
            "sentiment_label": sentiment_label,
            "active_channels": len(active_channels),
            "message_count_estimate": len(messages),
            "channel_signals": channel_signals,
            "alerts": alerts[:10],
        }

    # ── Data fetchers ──────────────────────────────────────

    def _fetch_telegram_api(self) -> dict | None:
        """
        Fetch messages from Telegram channels via Bot API.
        Only works if TELEGRAM_BOT_TOKEN is configured.
        Monitors public channels only.
        """
        if not self._telegram_token:
            return None

        try:
            # Note: Telegram Bot API has limited ability to read public channels
            # This is a placeholder — actual implementation requires channel subscriptions
            # or use of TDLib (Telegram Desktop Library) for higher capability
            messages = []
            channels = set()

            for channel_name, signal_type in _CHANNELS.items():
                try:
                    # Attempt to fetch via Bot API (requires bot to be member of channel)
                    url = (
                        f"https://api.telegram.org/bot{self._telegram_token}/"
                        f"getChat?chat_id=@{channel_name}"
                    )
                    req = urllib.request.Request(url)
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        result = _json.loads(resp.read().decode())
                        # If successful, mark channel as active
                        if result.get("ok"):
                            channels.add(channel_name)
                except Exception:
                    pass

            if channels:
                logger.debug(
                    "TelegramAgent: Telegram API found %d active channels", len(channels)
                )
                return {
                    "messages": messages,
                    "channels": channels,
                    "channel_signals": {},
                }
        except Exception as exc:
            logger.debug("TelegramAgent: Telegram API error — %s", exc)

        return None

    def _fetch_telemetrio(self) -> dict | None:
        """
        Fetch public channel statistics from Telemetrio.
        This is a free public stats API (may have limitations).
        """
        try:
            url = "https://telemetrio.com/channels_stats"
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "NexusTrader/1.0"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = _json.loads(resp.read().decode())

            messages = []
            channels = set()

            # Parse channel stats
            for channel_name in _CHANNELS.keys():
                for entry in data.get("channels", []):
                    if channel_name.lower() in entry.get("name", "").lower():
                        channels.add(channel_name)
                        # Create synthetic message from stats
                        msg_count = entry.get("messages", 0)
                        members = entry.get("members", 0)
                        messages.append(
                            {
                                "text": f"Channel {channel_name}: {msg_count} messages, {members} members",
                                "channel": channel_name,
                            }
                        )

            if channels:
                logger.debug(
                    "TelegramAgent: Telemetrio found %d channels", len(channels)
                )
                return {"messages": messages, "channels": channels}
        except Exception as exc:
            logger.debug("TelegramAgent: Telemetrio fetch failed — %s", exc)

        return None

    def _fetch_tme_previews(self) -> dict | None:
        """
        Fetch messages from t.me preview pages for public channels.
        t.me/s/CHANNEL shows public posts without requiring Telegram app.
        """
        try:
            messages = []
            channels = set()

            for channel_name in _CHANNELS.keys():
                try:
                    url = f"https://t.me/s/{channel_name}"
                    req = urllib.request.Request(
                        url,
                        headers={"User-Agent": "Mozilla/5.0"},
                    )
                    with urllib.request.urlopen(req, timeout=8) as resp:
                        html = resp.read().decode("utf-8", errors="ignore")

                    # Extract message texts from HTML
                    # t.me uses data attributes with message text
                    msg_texts = re.findall(
                        r'class="tgme_widget_message_text"[^>]*>([^<]+)<', html
                    )
                    if not msg_texts:
                        msg_texts = re.findall(r'<span[^>]*>([^<]*(?:bull|bear|buy|sell|pump|dump|moon|crash)[^<]*)</span>', html, re.IGNORECASE)

                    if msg_texts:
                        channels.add(channel_name)
                        for text in msg_texts[:5]:  # Top 5 messages per channel
                            messages.append(
                                {
                                    "text": text.strip(),
                                    "channel": channel_name,
                                }
                            )
                except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
                    logger.debug("TelegramAgent: t.me/%s not accessible", channel_name)
                except Exception as exc:
                    logger.debug("TelegramAgent: t.me preview error — %s", exc)

            if channels:
                logger.debug("TelegramAgent: t.me previews found %d channels", len(channels))
                return {"messages": messages, "channels": channels}
        except Exception as exc:
            logger.debug("TelegramAgent: t.me preview fetch failed — %s", exc)

        return None

    # ── Helpers ────────────────────────────────────────────

    def _score_text(self, text: str) -> tuple[float, float]:
        """Score text sentiment using ModelRegistry scorer."""
        if not self._scorer:
            try:
                self._scorer = get_model_registry().get_scorer("telegram")
            except Exception:
                from core.ai.model_registry import _VaderScorer
                self._scorer = _VaderScorer()

        try:
            results = self._scorer.score([text])
            if results:
                sig, conf = results[0]
                return float(sig), float(conf)
        except Exception:
            pass

        return 0.0, 0.0

    def _is_pump_signal(self, text: str) -> bool:
        """Detect pump/buy signal keywords in message text."""
        pump_keywords = {
            "buy", "bullish", "pump", "moon", "breakout", "accumulate",
            "signal", "long", "entry", "target",
        }
        text_lower = text.lower()
        return any(kw in text_lower for kw in pump_keywords)


# ── Module-level singleton ────────────────────────────────────
telegram_agent: TelegramSentimentAgent | None = None
