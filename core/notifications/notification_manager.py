# ============================================================
# NEXUS TRADER — Notification Manager
#
# Central dispatch hub for all trade/system notifications.
# Responsibilities:
#   1. Subscribe to EventBus topics that require notifications
#   2. Deduplicate notifications (no flood on repeated events)
#   3. Route to enabled channels (WhatsApp, Telegram, SMS, Email)
#   4. Log every notification attempt
#   5. Respect user preferences (per-type enable/disable)
#
# Channel priority:
#   Primary   — WhatsApp (Twilio)
#   Secondary — Telegram
#   Tertiary  — Email
#   Optional  — SMS (brief alerts only)
#
# Deduplication:
#   A (topic, key) pair is suppressed if last notification for
#   that pair was within the dedup_window_seconds (default 60s).
#   Key is typically the symbol + direction.
#
# Thread safety:
#   All public methods are thread-safe via RLock.
#   Channel sends happen in a background thread to avoid blocking.
# ============================================================
from __future__ import annotations

import hashlib
import logging
import queue
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from core.event_bus import bus, Topics, Event
from core.notifications import notification_templates as tpl

logger = logging.getLogger(__name__)

# ── Default notification preferences ──────────────────────────
# Each key maps to whether that notification type is enabled by default.
_DEFAULT_PREFS: dict[str, bool] = {
    "trade_opened":     True,
    "trade_closed":     True,
    "trade_stopped":    True,
    "trade_rejected":   False,  # can be noisy — off by default
    "trade_modified":   False,
    "strategy_signal":  False,  # pre-trade signals — off by default
    "risk_warning":     True,
    "market_condition": False,
    "system_error":     True,
    "system_alert":     False,
    "emergency_stop":   True,
    "daily_summary":    True,
    "health_check":     True,   # 4-hour system health check — on by default
}

_DEFAULT_HEALTH_CHECK_INTERVAL_H = 6   # default: every 6 hours
_VALID_HEALTH_CHECK_HOURS = (1, 2, 3, 4, 6, 12, 24)

_DEDUP_WINDOW_S = 60        # seconds to suppress duplicate notifications
_THREAD_POOL_SIZE = 4       # concurrent channel sends
_RETRY_MAX_ATTEMPTS = 3     # maximum retry attempts for failed sends
_RETRY_BACKOFF_BASE = 5     # base backoff in seconds (5s, 10s, 20s)
_TWILIO_RATE_LIMIT = 28     # messages per minute (2 msg buffer under 30/min limit)


@dataclass
class _NotifRecord:
    """Tracks a sent notification for deduplication."""
    template:   str
    dedup_key:  str
    sent_at:    float = field(default_factory=time.time)
    success:    bool  = False
    channels:   list[str] = field(default_factory=list)


@dataclass
class _RetryTask:
    """Queued task for retry with backoff."""
    channel: object
    content: dict[str, str]
    record: _NotifRecord
    attempt: int = 0
    next_retry_at: float = field(default_factory=time.time)


class NotificationManager:
    """
    Singleton notification hub.

    Usage
    -----
    mgr = NotificationManager()
    mgr.configure(config_dict)   # pass channel configs from settings
    mgr.start()                  # subscribe to EventBus

    # Manual dispatch (for testing or programmatic use)
    mgr.notify("trade_opened", data_dict, dedup_key="BTCUSDT_long")
    """

    def __init__(self):
        self._lock      = threading.RLock()
        self._channels: list = []
        self._prefs:    dict[str, bool] = dict(_DEFAULT_PREFS)
        self._history:  list[_NotifRecord] = []
        self._dedup:    dict[str, float] = {}   # dedup_key → last sent timestamp
        self._executor  = ThreadPoolExecutor(max_workers=_THREAD_POOL_SIZE,
                                             thread_name_prefix="notif")
        self._running   = False
        self._subscriptions: list[tuple[str, callable]] = []

        # Retry queue and worker
        self._retry_queue: queue.Queue = queue.Queue()
        self._retry_worker_thread: Optional[threading.Thread] = None

        # Daily summary scheduling
        self._daily_summary_enabled: bool = True
        self._daily_summary_hour: int = 22  # 10pm local time
        self._daily_summary_timer: Optional[threading.Timer] = None

        # Health check scheduling — interval is user-configurable (hours)
        self._health_check_enabled: bool = True
        self._health_check_interval_h: int = _DEFAULT_HEALTH_CHECK_INTERVAL_H
        self._health_check_timer: Optional[threading.Timer] = None

        # Last known feed status — updated via FEED_STATUS events
        self._feed_active: bool = False

        # Twilio rate limit tracking
        self._twilio_message_count: int = 0
        self._twilio_window_start: float = time.time()

        # Delivery metrics
        self._delivery_stats: dict = {
            "total_sent": 0,
            "total_failed": 0,
            "total_retried": 0,
            "success_rate": 0.0,
        }

    # ── Configuration ─────────────────────────────────────────

    def configure(self, config: dict) -> None:
        """
        Build channels from settings dict.

        config expected keys:
            whatsapp   : dict  (WhatsApp channel config)
            telegram   : dict  (Telegram channel config)
            email      : dict  (Email channel config)
            sms        : dict  (SMS channel config)
            preferences: dict  (override notification type enable/disable)
            dedup_window_seconds: int
        """
        global _DEDUP_WINDOW_S
        _DEDUP_WINDOW_S = int(config.get("dedup_window_seconds", _DEDUP_WINDOW_S))

        channels: list = []

        # WhatsApp
        wa_cfg = config.get("whatsapp", {})
        if wa_cfg.get("enabled", False):
            from core.notifications.channels.whatsapp_channel import WhatsAppChannel
            channels.append(WhatsAppChannel(wa_cfg))
            logger.info("NotificationManager: WhatsApp channel enabled")

        # Telegram
        tg_cfg = config.get("telegram", {})
        if tg_cfg.get("enabled", False):
            from core.notifications.channels.telegram_channel import TelegramChannel
            channels.append(TelegramChannel(tg_cfg))
            logger.info("NotificationManager: Telegram channel enabled")

        # Email
        em_cfg = config.get("email", {})
        if em_cfg.get("enabled", False):
            from core.notifications.channels.email_channel import EmailChannel
            channels.append(EmailChannel(em_cfg))
            logger.info("NotificationManager: Email channel enabled")

        # SMS
        sms_cfg = config.get("sms", {})
        if sms_cfg.get("enabled", False):
            from core.notifications.channels.sms_channel import SMSChannel
            channels.append(SMSChannel(sms_cfg))
            logger.info("NotificationManager: SMS channel enabled")

        # Gemini (Google Account / Gmail with optional AI enrichment)
        gm_cfg = config.get("gemini", {})
        if gm_cfg.get("enabled", False):
            from core.notifications.channels.gemini_channel import GeminiChannel
            # Inject Gemini API key from the AI config section if not explicitly set
            if gm_cfg.get("ai_enrich") and not gm_cfg.get("gemini_api_key"):
                try:
                    from core.security.key_vault import key_vault
                    gm_api_key = key_vault.load("ai.gemini_api_key")
                    if gm_api_key:
                        gm_cfg = dict(gm_cfg)   # copy so we don't mutate source
                        gm_cfg["gemini_api_key"] = gm_api_key
                except Exception:
                    pass
            channels.append(GeminiChannel(gm_cfg))
            logger.info("NotificationManager: Gemini channel enabled")

        # User preferences
        pref_override = config.get("preferences", {})
        for k, v in pref_override.items():
            if k in self._prefs:
                self._prefs[k] = bool(v)

        # Health check interval (hours) — read separately, not a boolean pref
        raw_h = pref_override.get("health_check_interval_hours", _DEFAULT_HEALTH_CHECK_INTERVAL_H)
        try:
            self._health_check_interval_h = int(raw_h)
        except (TypeError, ValueError):
            self._health_check_interval_h = _DEFAULT_HEALTH_CHECK_INTERVAL_H

        with self._lock:
            self._channels = channels

        logger.info(
            "NotificationManager: configured %d channel(s)", len(channels)
        )

    def _on_settings_changed(self, event: Event) -> None:
        """Reconfigure channels when user saves notification settings."""
        try:
            changed = event.data if hasattr(event, "data") else (event if isinstance(event, dict) else {})
            # Only reconfigure if a notification-related key was changed
            notif_keys = [k for k in changed if isinstance(k, str) and k.startswith("notifications.")]
            if not notif_keys:
                return

            from config.settings import settings
            import copy
            notif_cfg = copy.deepcopy(settings.get_section("notifications"))

            # Inject secrets from vault (same as main.py startup path)
            try:
                from core.security.key_vault import key_vault
                email_pass = key_vault.load("notifications.email_password")
                if email_pass:
                    em_cfg = notif_cfg.get("email", {})
                    em_cfg["password"] = email_pass
                    notif_cfg["email"] = em_cfg

                tg_token = key_vault.load("notifications.telegram_token")
                if tg_token:
                    tg_cfg = notif_cfg.get("telegram", {})
                    tg_cfg["bot_token"] = tg_token
                    notif_cfg["telegram"] = tg_cfg

                twilio_sid = key_vault.load("notifications.twilio_sid")
                twilio_token = key_vault.load("notifications.twilio_token")
                if twilio_sid and twilio_token:
                    for ch_key in ("whatsapp", "sms"):
                        ch_cfg = notif_cfg.get(ch_key, {})
                        ch_cfg.update({"account_sid": twilio_sid, "auth_token": twilio_token})
                        notif_cfg[ch_key] = ch_cfg

                gemini_pass = key_vault.load("notifications.gemini_password")
                if gemini_pass:
                    gm_cfg = notif_cfg.get("gemini", {})
                    gm_cfg["password"] = gemini_pass
                    notif_cfg["gemini"] = gm_cfg
            except Exception:
                pass

            self.configure(notif_cfg)
            logger.info(
                "NotificationManager: reconfigured after settings change — %d channel(s)",
                len(self._channels),
            )
        except Exception as exc:
            logger.error("NotificationManager: failed to reconfigure on settings change: %s", exc)

    # ── Lifecycle ─────────────────────────────────────────────

    def start(self) -> None:
        """Subscribe to all relevant EventBus topics and start background workers."""
        if self._running:
            return
        self._running = True

        subs = [
            (Topics.TRADE_OPENED,       self._on_trade_opened),
            (Topics.TRADE_CLOSED,       self._on_trade_closed),
            (Topics.ORDER_FILLED,       self._on_order_filled),
            (Topics.SIGNAL_REJECTED,    self._on_signal_rejected),
            (Topics.SIGNAL_CONFIRMED,   self._on_signal_confirmed),
            (Topics.DRAWDOWN_ALERT,     self._on_drawdown_alert),
            (Topics.RISK_LIMIT_HIT,     self._on_risk_limit),
            (Topics.EMERGENCY_STOP,     self._on_emergency_stop),
            (Topics.REGIME_CHANGED,     self._on_regime_changed),
            (Topics.EXCHANGE_ERROR,     self._on_exchange_error),
            (Topics.SYSTEM_ALERT,       self._on_system_alert),
            (Topics.CANDIDATE_APPROVED, self._on_candidate_approved),
            (Topics.FEED_STATUS,        self._on_feed_status),
            (Topics.SETTINGS_CHANGED,   self._on_settings_changed),
        ]

        for topic, handler in subs:
            bus.subscribe(topic, handler)
            self._subscriptions.append((topic, handler))

        # Start retry worker thread
        self._retry_worker_thread = threading.Thread(
            target=self._retry_worker, daemon=True, name="notif-retry-worker"
        )
        self._retry_worker_thread.start()

        # Schedule daily summary
        if self._daily_summary_enabled:
            self._schedule_daily_summary()

        # Schedule health check (interval configured by user, default 6h)
        if self._health_check_enabled:
            self._schedule_health_check()

        logger.info("NotificationManager: started, subscribed to %d topics", len(subs))

    def stop(self) -> None:
        """Unsubscribe and shutdown executor."""
        for topic, handler in self._subscriptions:
            bus.unsubscribe(topic, handler)
        self._subscriptions.clear()

        # Cancel daily summary timer if active
        if self._daily_summary_timer:
            self._daily_summary_timer.cancel()
            self._daily_summary_timer = None

        # Cancel health check timer if active
        if self._health_check_timer:
            self._health_check_timer.cancel()
            self._health_check_timer = None

        # Signal retry worker to stop by putting None sentinel
        self._retry_queue.put(None)

        self._executor.shutdown(wait=False)
        self._running = False
        logger.info("NotificationManager: stopped")

    # ── Public API ────────────────────────────────────────────

    def notify(
        self,
        template_name: str,
        data: dict,
        dedup_key: Optional[str] = None,
        channels: Optional[list[str]] = None,
    ) -> bool:
        """
        Render and dispatch a notification.

        template_name : key from notification_templates.TEMPLATES
        data          : data dict passed to template
        dedup_key     : if set, suppresses duplicate within dedup window
        channels      : if set, only use these channel names; else use all

        Returns True if at least one channel succeeded.
        """
        # Check preference
        if not self._prefs.get(template_name, True):
            logger.debug("NotificationManager: type '%s' disabled by preferences", template_name)
            return False

        # Deduplication
        if dedup_key:
            full_key = f"{template_name}:{dedup_key}"
            with self._lock:
                last = self._dedup.get(full_key, 0.0)
                if time.time() - last < _DEDUP_WINDOW_S:
                    logger.debug(
                        "NotificationManager: suppressed duplicate '%s' key=%s",
                        template_name, dedup_key,
                    )
                    return False
                self._dedup[full_key] = time.time()

        # Render template
        try:
            content = tpl.render(template_name, data)
        except Exception as exc:
            logger.error("NotificationManager: template render failed — %s", exc)
            return False

        # Dispatch to channels asynchronously
        with self._lock:
            active_channels = [
                ch for ch in self._channels
                if channels is None or ch.name in channels
            ]

        if not active_channels:
            logger.debug("NotificationManager: no active channels for '%s'", template_name)
            return False

        record = _NotifRecord(
            template=template_name,
            dedup_key=dedup_key or "",
        )

        futures = []
        for ch in active_channels:
            fut = self._executor.submit(
                self._send_on_channel, ch, content, record
            )
            futures.append(fut)

        # Log immediately (don't wait for futures)
        with self._lock:
            self._history.append(record)
            if len(self._history) > 500:
                self._history.pop(0)

        logger.info(
            "NotificationManager: dispatched '%s' to %d channel(s) | key=%s",
            template_name, len(active_channels), dedup_key or "—",
        )
        return True

    def set_preference(self, notification_type: str, enabled: bool) -> None:
        """Enable or disable a notification type at runtime."""
        with self._lock:
            self._prefs[notification_type] = enabled

    def set_daily_summary_hour(self, hour: int) -> None:
        """Set the hour (0-23) for daily summary notifications."""
        if not 0 <= hour <= 23:
            raise ValueError("hour must be 0-23")
        with self._lock:
            self._daily_summary_hour = hour
        # Reschedule if timer is active
        if self._daily_summary_timer:
            self._daily_summary_timer.cancel()
            if self._running and self._daily_summary_enabled:
                self._schedule_daily_summary()

    def set_health_check_interval(self, hours: int) -> None:
        """Set the health-check send interval (hours).  Reschedules immediately."""
        if hours not in _VALID_HEALTH_CHECK_HOURS:
            raise ValueError(
                f"hours must be one of {_VALID_HEALTH_CHECK_HOURS}, got {hours}"
            )
        with self._lock:
            self._health_check_interval_h = hours
        # Cancel current timer and restart with new interval
        if self._health_check_timer:
            self._health_check_timer.cancel()
            self._health_check_timer = None
        if self._running and self._health_check_enabled:
            self._schedule_health_check()
        logger.info(
            "NotificationManager: health check interval set to %d hour(s)", hours
        )

    def get_health_check_interval(self) -> int:
        """Return current health-check interval in hours."""
        with self._lock:
            return self._health_check_interval_h

    def get_delivery_stats(self) -> dict:
        """Return delivery statistics."""
        with self._lock:
            return dict(self._delivery_stats)

    def get_history(self, limit: int = 50) -> dict:
        """Return recent notification history and stats as a dict."""
        with self._lock:
            items = list(reversed(self._history[-limit:]))
            stats = dict(self._delivery_stats)
        return {
            "notifications": [
                {
                    "template": r.template,
                    "dedup_key": r.dedup_key,
                    "sent_at": datetime.fromtimestamp(r.sent_at, tz=timezone.utc).isoformat(),
                    "success": r.success,
                    "channels": r.channels,
                }
                for r in items
            ],
            "stats": stats,
        }

    def get_channel_count(self) -> int:
        """Return number of configured channels."""
        with self._lock:
            return len(self._channels)

    def test_all_channels(self) -> dict[str, bool]:
        """Test each configured channel, returns {channel_name: success}."""
        results: dict[str, bool] = {}
        with self._lock:
            channels = list(self._channels)
        for ch in channels:
            try:
                results[ch.name] = ch.test()
            except Exception as exc:
                logger.error("NotificationManager: test failed for %s — %s", ch.name, exc)
                results[ch.name] = False
        return results

    # ── Channel send ──────────────────────────────────────────

    def _send_on_channel(
        self, channel, content: dict[str, str], record: _NotifRecord
    ) -> bool:
        """Run in thread pool — attempt send on a single channel."""
        try:
            # Check Twilio rate limits for WhatsApp and SMS
            if channel.name in ("whatsapp", "sms"):
                if not self._check_twilio_rate_limit():
                    logger.warning(
                        "NotificationManager: %s rate limited, queuing for retry",
                        channel.name,
                    )
                    # Queue for retry in 61 seconds
                    retry_task = _RetryTask(
                        channel=channel,
                        content=content,
                        record=record,
                        attempt=0,
                        next_retry_at=time.time() + 61,
                    )
                    self._retry_queue.put(retry_task)
                    with self._lock:
                        self._delivery_stats["total_retried"] += 1
                    return False

            # Choose message format based on channel
            if channel.name in ("whatsapp", "telegram", "sms"):
                body = content.get("short", content.get("body", ""))
            else:
                body = content.get("body", "")
            subject = content.get("subject", "NexusTrader Notification")

            # Email-capable channels receive a rich HTML body when available
            if channel.name in ("email", "gemini") and content.get("html_body"):
                ok = channel.send(body, subject=subject, html_body=content["html_body"])
            else:
                ok = channel.send(body, subject=subject)
            # Update record (thread-safe via GIL for these simple operations)
            if ok:
                record.success = True
                with self._lock:
                    self._delivery_stats["total_sent"] += 1
            else:
                with self._lock:
                    self._delivery_stats["total_failed"] += 1
                # Queue for retry with exponential backoff
                retry_task = _RetryTask(
                    channel=channel,
                    content=content,
                    record=record,
                    attempt=0,
                    next_retry_at=time.time() + _RETRY_BACKOFF_BASE,
                )
                self._retry_queue.put(retry_task)
                with self._lock:
                    self._delivery_stats["total_retried"] += 1

            if channel.name not in record.channels:
                record.channels.append(channel.name)
            if ok:
                logger.debug("NotificationManager: ✓ %s channel success", channel.name)
            else:
                logger.warning("NotificationManager: ✗ %s channel failed, queued for retry", channel.name)
            return ok
        except Exception as exc:
            logger.error(
                "NotificationManager: channel %s raised exception — %s",
                channel.name, exc,
            )
            with self._lock:
                self._delivery_stats["total_failed"] += 1
            return False

    # ── Retry and rate limiting ───────────────────────────────

    def _check_twilio_rate_limit(self) -> bool:
        """
        Check if Twilio rate limit (28 msgs/min) is exceeded.
        Returns True if send is allowed, False if rate limited.
        """
        now = time.time()
        elapsed = now - self._twilio_window_start

        # Reset window if 60 seconds have passed
        if elapsed >= 60:
            self._twilio_message_count = 0
            self._twilio_window_start = now

        # Check if at limit
        if self._twilio_message_count >= _TWILIO_RATE_LIMIT:
            return False

        self._twilio_message_count += 1
        return True

    def _retry_worker(self) -> None:
        """
        Background worker that processes the retry queue.
        Waits until next_retry_at, then attempts resend with exponential backoff.
        """
        logger.debug("NotificationManager: retry worker started")
        while True:
            try:
                task = self._retry_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            # Sentinel to stop worker
            if task is None:
                logger.debug("NotificationManager: retry worker stopping")
                break

            # Wait until next_retry_at
            delay = task.next_retry_at - time.time()
            if delay > 0:
                time.sleep(delay)

            # Attempt retry
            task.attempt += 1
            if task.attempt > _RETRY_MAX_ATTEMPTS:
                logger.critical(
                    "NotificationManager: %s retry failed after %d attempts for %s",
                    task.channel.name, task.attempt - 1, task.record.template,
                )
                continue

            try:
                ok = self._send_on_channel(task.channel, task.content, task.record)
                if ok:
                    logger.info(
                        "NotificationManager: %s retry succeeded on attempt %d",
                        task.channel.name, task.attempt,
                    )
                else:
                    # Reschedule with next backoff
                    backoff = _RETRY_BACKOFF_BASE * (2 ** task.attempt)
                    task.next_retry_at = time.time() + backoff
                    self._retry_queue.put(task)
                    logger.debug(
                        "NotificationManager: %s rescheduled retry in %d seconds",
                        task.channel.name, backoff,
                    )
            except Exception as exc:
                logger.error(
                    "NotificationManager: retry worker exception for %s — %s",
                    task.channel.name, exc,
                )

    def _schedule_daily_summary(self) -> None:
        """
        Schedule daily summary to fire at the configured hour (local time).
        """
        from datetime import timedelta
        now = datetime.now()
        target = now.replace(hour=self._daily_summary_hour, minute=0, second=0, microsecond=0)

        # If target time has passed today, schedule for tomorrow
        if target <= now:
            target = target + timedelta(days=1)

        delay = (target - now).total_seconds()

        if self._daily_summary_timer:
            self._daily_summary_timer.cancel()

        self._daily_summary_timer = threading.Timer(delay, self._send_daily_summary)
        self._daily_summary_timer.daemon = True
        self._daily_summary_timer.start()

        logger.debug(
            "NotificationManager: daily summary scheduled for %s (in %.0f seconds)",
            target.isoformat(), delay,
        )

    def _send_daily_summary(self) -> None:
        """
        Gather today's stats and send daily summary notification.
        Reschedules the next daily summary.
        """
        try:
            now     = datetime.now(timezone.utc)
            today   = now.strftime("%Y-%m-%d")
            data: dict = {"date": today}

            # ── Collect live trading stats ────────────────────────────────────
            try:
                from core.execution.paper_executor import paper_executor as _pe
                stats = _pe.get_stats()

                # Filter closed trades to today only
                today_trades = [
                    t for t in _pe._closed_trades
                    if isinstance(t.get("closed_at", ""), str)
                    and t["closed_at"].startswith(today)
                ]
                wins   = sum(1 for t in today_trades if (t.get("pnl_pct") or 0) > 0)
                losses = len(today_trades) - wins
                daily_pnl = sum(float(t.get("pnl_usdt") or 0) for t in today_trades)

                # Compute open equity for today P&L
                _open_flat = [p for pos_list in _pe._positions.values() for p in pos_list]
                _unrealized = sum(p.size_usdt * (p.unrealized_pnl / 100) for p in _open_flat)
                daily_pnl_total = daily_pnl + _unrealized

                _initial = _pe._initial_capital or _pe._capital or 1.0
                daily_pnl_pct = round(daily_pnl_total / _initial * 100, 2)

                # Current equity
                _locked    = sum(p.size_usdt for p in _open_flat)
                _mtm       = sum(p.size_usdt * (1 + p.unrealized_pnl / 100) for p in _open_flat)
                equity     = round((_pe._capital - _locked) + _mtm, 2)

                # Overall win rate (all time, not just today — meaningful for daily review)
                win_rate_frac = stats.get("win_rate", 0.0) / 100.0  # get_stats returns 0-100

                # Current regime (best-effort)
                current_regime = "—"
                try:
                    from core.scanning.scanner import scanner as _scanner
                    if hasattr(_scanner, "_last_regime"):
                        current_regime = _scanner._last_regime or "—"
                except Exception:
                    pass

                data.update({
                    "total_trades": len(today_trades),
                    "wins":         wins,
                    "losses":       losses,
                    "daily_pnl":    round(daily_pnl_total, 2),
                    "daily_pnl_pct":daily_pnl_pct,
                    "win_rate":     win_rate_frac,
                    "equity":       equity,
                    "current_regime": current_regime,
                })
            except Exception as _stats_exc:
                logger.debug("NotificationManager: daily summary stats error — %s", _stats_exc)
                data.setdefault("total_trades", 0)
                data.setdefault("wins",         0)
                data.setdefault("losses",       0)
                data.setdefault("daily_pnl",    0.0)
                data.setdefault("daily_pnl_pct",0.0)
                data.setdefault("win_rate",     0.0)
                data.setdefault("equity",       0.0)
                data.setdefault("current_regime","—")

            # Dispatch notification
            self.notify("daily_summary", data=data, dedup_key=None)
            logger.info("NotificationManager: daily summary sent")
        except Exception as exc:
            logger.error("NotificationManager: daily summary failed — %s", exc)
        finally:
            # Reschedule for next day
            if self._running and self._daily_summary_enabled:
                self._schedule_daily_summary()

    # ── Health check ──────────────────────────────────────────

    def _schedule_health_check(self) -> None:
        """Schedule next health check in _HEALTH_CHECK_INTERVAL_S seconds."""
        if self._health_check_timer:
            self._health_check_timer.cancel()

        interval_s = self._health_check_interval_h * 3600
        self._health_check_timer = threading.Timer(interval_s, self._send_health_check)
        self._health_check_timer.daemon = True
        self._health_check_timer.start()

        logger.debug(
            "NotificationManager: health check scheduled in %d hour(s)",
            self._health_check_interval_h,
        )

    def _send_health_check(self) -> None:
        """Gather system state and dispatch a health_check notification."""
        try:
            data = self._collect_health_data()
            self.notify("health_check", data=data, dedup_key=None)
            logger.info("NotificationManager: health check sent")
        except Exception as exc:
            logger.error("NotificationManager: health check failed — %s", exc)
        finally:
            # Reschedule whether or not this send succeeded
            if self._running and self._health_check_enabled:
                self._schedule_health_check()

    def _collect_health_data(self) -> dict:
        """
        Collect live system state for the health check message.
        Each section is wrapped in try/except so a partial failure
        never blocks the whole notification.
        """
        data: dict = {}

        # ── IDSS Scanner ──────────────────────────────────────
        try:
            from core.scanning.scanner import scanner as _scanner
            data["scanner_status"] = "Running" if _scanner._running else "Stopped"
            last_scan = _scanner._last_scan_at
            if last_scan is not None:
                age_s = (datetime.now(timezone.utc) - last_scan.replace(tzinfo=timezone.utc)).total_seconds()
                if age_s < 120:
                    data["last_scan_ago"] = "just now"
                elif age_s < 3600:
                    data["last_scan_ago"] = f"{int(age_s // 60)}m ago"
                else:
                    h = int(age_s // 3600)
                    m = int((age_s % 3600) // 60)
                    data["last_scan_ago"] = f"{h}h {m}m ago" if m else f"{h}h ago"
            else:
                data["last_scan_ago"] = "not yet run"
        except Exception:
            data["scanner_status"] = "Unknown"
            data["last_scan_ago"] = "Unknown"

        # ── Exchange (Bybit) connectivity ─────────────────────
        try:
            from core.market_data.exchange_manager import exchange_manager as _em
            if _em.is_connected():
                ex = _em.get_exchange()
                ex_name = ex.id.capitalize() if ex else "Exchange"
                data["exchange_status"] = f"Connected ({ex_name})"
            else:
                data["exchange_status"] = "Disconnected"
        except Exception:
            data["exchange_status"] = "Unknown"

        # ── Data Feed ─────────────────────────────────────────
        data["feed_status"] = "Active" if self._feed_active else "Inactive"

        # ── AI Provider ───────────────────────────────────────
        try:
            from config.settings import settings as _s
            provider = _s.get("ai.active_provider", "Unknown")
            model    = _s.get("ai.ollama_model", "")
            if "ollama" in provider.lower() or "local" in provider.lower():
                data["ai_status"] = f"Online (Ollama/{model})" if model else "Online (Ollama)"
            elif provider and provider != "Unknown":
                data["ai_status"] = f"Online ({provider})"
            else:
                data["ai_status"] = "Unknown"
        except Exception:
            data["ai_status"] = "Unknown"

        # ── Portfolio / Performance ───────────────────────────
        try:
            from core.execution.paper_executor import paper_executor as _pe
            stats = _pe.get_stats()

            # Compute true total equity = free cash + mark-to-market value of open positions.
            # _capital alone is settled cash only — it does NOT include unrealized P&L.
            # This matches the Dashboard's equity figure.
            _open_flat = [p for pos_list in _pe._positions.values() for p in pos_list]
            _locked     = sum(p.size_usdt for p in _open_flat)
            _mtm_value  = sum(p.size_usdt * (1 + p.unrealized_pnl / 100) for p in _open_flat)
            _total_equity = (_pe._capital - _locked) + _mtm_value

            # Unrealized P&L in USDT across all open positions
            _unrealized_usdt = sum(
                p.size_usdt * (p.unrealized_pnl / 100) for p in _open_flat
            )

            data["portfolio_value"]  = round(_total_equity, 2)
            data["available_cash"]   = round(_pe.available_capital, 2)
            data["win_rate"]         = stats.get("win_rate", 0.0)
            data["total_trades"]     = stats.get("total_trades", 0)
            data["open_positions"]   = stats.get("open_positions", 0)

            # Today's P&L = closed trades realised today + current unrealized P&L on open positions.
            # This mirrors what the Dashboard equity curve reflects in real time.
            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            today_pnl_closed = 0.0
            for t in _pe._closed_trades:
                closed_at = t.get("closed_at", "")
                if isinstance(closed_at, str) and closed_at.startswith(today_str):
                    today_pnl_closed += float(t.get("pnl_usdt") or 0)
            today_pnl = today_pnl_closed + _unrealized_usdt
            data["today_pnl"] = round(today_pnl, 2)

            # Today P&L as % of initial capital (so the % reflects gain/loss vs starting equity)
            _initial = _pe._initial_capital or _pe._capital or 1.0
            data["today_pnl_pct"] = round(today_pnl / _initial * 100, 2)

        except Exception as exc:
            logger.debug("NotificationManager: health check portfolio data error — %s", exc)
            data.setdefault("portfolio_value", 0.0)
            data.setdefault("available_cash",  0.0)
            data.setdefault("win_rate",        0.0)
            data.setdefault("total_trades",    0)
            data.setdefault("open_positions",  0)
            data.setdefault("today_pnl",       0.0)
            data.setdefault("today_pnl_pct",   0.0)

        # ── Open Trades Detail ─────────────────────────────────
        try:
            from core.execution.paper_executor import paper_executor as _pe_open
            _all_open = [
                p for pos_list in _pe_open._positions.values() for p in pos_list
            ]
            open_detail = []
            for pos in _all_open:
                d = pos.to_dict()
                # Augment with live fields not in to_dict()
                d["trailing_stop_pct"] = getattr(pos, "trailing_stop_pct", None)
                d["bars_held"]         = getattr(pos, "bars_held", None)
                d["highest_price"]     = getattr(pos, "highest_price", None)
                d["lowest_price"]      = getattr(pos, "lowest_price", None)
                open_detail.append(d)
            data["open_trades_detail"] = open_detail
        except Exception as exc:
            logger.debug("NotificationManager: open trades detail error — %s", exc)
            data.setdefault("open_trades_detail", [])

        # ── Recent Closed Trades Detail (last 10) ──────────────
        try:
            from core.execution.paper_executor import paper_executor as _pe_closed
            data["closed_trades_detail"] = [
                dict(t) for t in list(_pe_closed._closed_trades)[-10:]
            ]
        except Exception as exc:
            logger.debug("NotificationManager: closed trades detail error — %s", exc)
            data.setdefault("closed_trades_detail", [])

        return data

    # ── EventBus handlers ─────────────────────────────────────

    def _on_trade_opened(self, event: Event) -> None:
        data = dict(event.data or {})
        sym  = data.get("symbol", "???")

        # ── Key normalisation ──────────────────────────────────
        # paper_executor stores keys that differ from the template's expected keys.
        # Populate the template keys without overwriting values already present.

        # "direction" → paper_executor uses "side" ("buy"/"sell"); template uses "direction"
        if "direction" not in data and "side" in data:
            data["direction"] = "long" if data["side"] == "buy" else "short"

        # "size" → paper_executor uses "size_usdt"; template uses "size"
        if "size" not in data and "size_usdt" in data:
            usdt = data["size_usdt"]
            data["size"] = f"${float(usdt):,.2f} USDT" if usdt else "—"

        # "confidence" → paper_executor uses "score" (0–1 float); template uses "confidence"
        if "confidence" not in data and "score" in data:
            data["confidence"] = float(data["score"] or 0.0)

        # "strategy" → paper_executor uses "models_fired" (list); template uses "strategy"
        if "strategy" not in data:
            mf = data.get("models_fired") or []
            if isinstance(mf, list) and mf:
                data["strategy"] = ", ".join(mf)
            elif isinstance(mf, str) and mf:
                data["strategy"] = mf

        # ── AI Analysis enrichment (entry quality only) ────────
        try:
            from core.analysis.trade_analysis_service import trade_analysis_service
            analysis = trade_analysis_service.build_trade_analysis(data)
            payload  = trade_analysis_service.generate_notification_payload(data, analysis)
            data.update(payload)
        except Exception as exc:
            logger.debug("NotificationManager: analysis enrichment failed — %s", exc)

        # Phase 2: use canonical renderer for notification
        try:
            from core.analysis.canonical_renderer import render_for_channel, MODE_NOTIF_OPEN
            rendered = render_for_channel(analysis, mode=MODE_NOTIF_OPEN, trade=data)
            data.update(payload)
            data["analysis_notification_lines"] = rendered.get("text_lines", [])
            data["analysis_summary_line"] = rendered.get("summary_line", "")
        except Exception as exc2:
            logger.debug("canonical_renderer open failed: %s", exc2)

        direction = data.get("direction", "long")
        self.notify(
            "trade_opened", data,
            dedup_key=f"{sym}_{direction}",
        )

    def _on_trade_closed(self, event: Event) -> None:
        data = dict(event.data or {})
        sym  = data.get("symbol", "???")

        # ── v1.2: Route partial_close to dedicated notification ───────────
        # partial_close events have exit_reason="partial_close".  They are
        # structurally different from a full close (no AI scorecard, different
        # message fields) and need a dedicated template so the email is clear.
        if data.get("exit_reason") == "partial_close":
            self._on_partial_exit(data)
            return

        # ── Key normalisation ──────────────────────────────────
        # paper_executor uses different key names than the trade_closed template expects.
        # Populate template keys without overwriting values already present.

        # "direction" → executor sends "side" ("buy"/"sell"); template uses "direction"
        if "direction" not in data and "side" in data:
            data["direction"] = "long" if data["side"] == "buy" else "short"

        # "pnl" → template reads data.get("pnl"), executor sends "pnl_usdt"
        if "pnl" not in data and "pnl_usdt" in data:
            data["pnl"] = data["pnl_usdt"]

        # "size" → executor sends "size_usdt"; template uses "size"
        if "size" not in data and "size_usdt" in data:
            usdt = data["size_usdt"]
            data["size"] = f"${float(usdt):,.2f} USDT" if usdt else "—"

        # "strategy" → executor sends "models_fired" (list); template uses "strategy"
        if "strategy" not in data:
            mf = data.get("models_fired") or []
            if isinstance(mf, list) and mf:
                data["strategy"] = ", ".join(mf)
            elif isinstance(mf, str) and mf:
                data["strategy"] = mf

        # "close_reason" → executor sends "exit_reason"; template uses "close_reason"
        if "close_reason" not in data and "exit_reason" in data:
            data["close_reason"] = data["exit_reason"]

        # "duration" → executor sends "duration_s" (integer seconds); template uses "duration"
        if "duration" not in data and "duration_s" in data:
            ds = int(data["duration_s"] or 0)
            if ds < 60:
                data["duration"] = f"{ds}s"
            elif ds < 3600:
                data["duration"] = f"{ds // 60}m {ds % 60}s"
            else:
                h = ds // 3600
                m = (ds % 3600) // 60
                data["duration"] = f"{h}h {m}m"

        # ── AI Analysis enrichment (full 4-score analysis on closed trade) ──
        analysis = None
        try:
            from core.analysis.trade_analysis_service import trade_analysis_service
            analysis = trade_analysis_service.build_trade_analysis(data)
            payload  = trade_analysis_service.generate_notification_payload(data, analysis)
            data.update(payload)
            # Persist feedback + trigger async AI explanation
            trade_analysis_service.on_trade_closed(data, analysis=analysis)
        except Exception as exc:
            logger.debug("NotificationManager: analysis enrichment (closed) failed — %s", exc)

        # Phase 2: use canonical renderer for notification
        try:
            from core.analysis.canonical_renderer import render_for_channel, MODE_NOTIF_CLOSED
            rendered = render_for_channel(analysis, mode=MODE_NOTIF_CLOSED, trade=data)
            data["analysis_notification_lines"] = rendered.get("text_lines", [])
            data["analysis_summary_line"] = rendered.get("summary_line", "")
        except Exception as exc2:
            logger.debug("canonical_renderer closed failed: %s", exc2)

        reason = data.get("close_reason", "")
        self.notify(
            "trade_closed", data,
            dedup_key=f"closed_{sym}_{reason}",
        )

    # ── v1.2 Partial exit notification ───────────────────────────────────

    def _on_partial_exit(self, data: dict) -> None:
        """
        Send a dedicated notification for a partial-close event (v1.2 exit logic).
        Called from _on_trade_closed() when exit_reason == "partial_close".

        Fields from paper_executor.partial_close():
          symbol, side, entry_price, exit_price, pnl_usdt, size_usdt (closed portion),
          entry_size_usdt (original), exit_size_usdt (portion closed), duration_s, regime
        """
        sym = data.get("symbol", "???")
        side = data.get("side", "buy")
        direction = "LONG" if side == "buy" else "SHORT"
        entry     = data.get("entry_price", 0.0)
        exit_p    = data.get("exit_price",  0.0)
        pnl_usdt  = float(data.get("pnl_usdt", 0.0) or 0.0)
        closed_sz = float(data.get("size_usdt", 0.0) or 0.0)
        orig_sz   = float(data.get("entry_size_usdt", closed_sz) or closed_sz)
        remaining = round(orig_sz - closed_sz, 2)
        regime    = data.get("regime", "—")
        tf        = data.get("timeframe", "30m")

        close_pct = round(closed_sz / orig_sz * 100) if orig_sz > 0 else 33

        notify_data = {
            "symbol":             sym,
            "direction":          direction,
            "timeframe":          tf,
            "regime":             regime,
            "entry_price":        entry,
            "exit_price":         exit_p,
            "pnl_usdt":           pnl_usdt,
            "pnl":                pnl_usdt,
            "closed_size_usdt":   round(closed_sz, 2),
            "remaining_size_usdt": remaining,
            "close_pct":          close_pct,
            "stop_now_breakeven": True,   # SL moved to entry by _breakeven_applied
            "models_fired":       data.get("models_fired", []),
            "strategy":           ", ".join(data.get("models_fired") or []),
        }
        self.notify(
            "partial_exit",
            notify_data,
            dedup_key=f"partial_{sym}_{direction}",
        )

    def _on_order_filled(self, event: Event) -> None:
        """Order filled — check if it's a stop-loss fill."""
        data = event.data or {}
        order_type = data.get("order_type", "")
        if "stop" in order_type.lower():
            sym  = data.get("symbol", "???")
            side = data.get("side", "")
            # Normalise "buy"/"sell" → "long"/"short" for consistent display
            direction = "long" if side == "buy" else ("short" if side == "sell" else side or "long")
            # loss/loss_pct: executor may send a negative realized_pnl; template expects
            # a positive USDT amount displayed with a leading minus sign via _fmt_price.
            # Keep as-is — _fmt_price and _fmt_pct handle sign display correctly.
            self.notify(
                "trade_stopped", {
                    "symbol":       sym,
                    "direction":    direction,
                    "entry_price":  data.get("entry_price"),
                    "stop_price":   data.get("fill_price", data.get("price")),
                    "loss":         data.get("realized_pnl"),
                    "loss_pct":     data.get("pnl_pct"),
                },
                dedup_key=f"stop_{sym}",
            )

    def _on_signal_rejected(self, event: Event) -> None:
        data = dict(event.data or {})
        sym  = data.get("symbol", "???")
        # Normalise score → confidence and models_fired → strategy
        if "confidence" not in data and "score" in data:
            data["confidence"] = float(data["score"] or 0.0)
        if "strategy" not in data:
            mf = data.get("models_fired") or []
            if isinstance(mf, list) and mf:
                data["strategy"] = ", ".join(mf)
            elif isinstance(mf, str) and mf:
                data["strategy"] = mf
        self.notify(
            "trade_rejected", data,
            dedup_key=f"rejected_{sym}_{data.get('strategy','')}",
        )

    def _on_signal_confirmed(self, event: Event) -> None:
        """Confluence-confirmed signal — pre-trade alert."""
        data = dict(event.data or {})
        sym  = data.get("symbol", "???")
        # Normalise score → confidence and models_fired → strategy/contributing_signals
        if "confidence" not in data and "score" in data:
            data["confidence"] = float(data["score"] or 0.0)
        if "strategy" not in data:
            mf = data.get("models_fired") or []
            if isinstance(mf, list) and mf:
                data["strategy"] = ", ".join(mf)
                data.setdefault("contributing_signals", mf)
            elif isinstance(mf, str) and mf:
                data["strategy"] = mf
        self.notify(
            "strategy_signal", data,
            dedup_key=f"signal_{sym}_{data.get('direction','')}",
        )

    def _on_drawdown_alert(self, event: Event) -> None:
        data = event.data or {}
        self.notify(
            "risk_warning", {
                "warning_type": "Drawdown Alert",
                "level":        data.get("level", "high"),
                "message":      data.get("message", "Drawdown threshold exceeded"),
                "current_value":data.get("drawdown_pct"),
                "threshold":    data.get("threshold"),
            },
            dedup_key="drawdown_alert",
        )

    def _on_risk_limit(self, event: Event) -> None:
        data = event.data or {}
        self.notify(
            "risk_warning", {
                "warning_type": "Risk Limit Hit",
                "level":        "critical",
                "message":      data.get("message", "Risk limit exceeded"),
                "current_value":data.get("current_value"),
                "threshold":    data.get("limit"),
            },
            dedup_key=f"risk_limit_{data.get('limit_type','general')}",
        )

    def _on_emergency_stop(self, event: Event) -> None:
        data = event.data or {}
        # Emergency stop bypasses dedup — always send
        self.notify(
            "emergency_stop", data,
            dedup_key=None,   # Never deduplicate emergency stops
        )

    def _on_regime_changed(self, event: Event) -> None:
        data = event.data or {}
        new_regime = data.get("new_regime", "—")
        confidence = data.get("confidence", 0.0)
        if confidence >= 0.65:  # Only notify on high-confidence regime changes
            self.notify(
                "market_condition", {
                    "condition":  f"Regime Change → {new_regime}",
                    "regime":     new_regime,
                    "confidence": confidence,
                    "message":    data.get("message", ""),
                },
                dedup_key=f"regime_{new_regime}",
            )

    def _on_exchange_error(self, event: Event) -> None:
        data = event.data or {}
        self.notify(
            "system_error", {
                "component": "Exchange Connection",
                "error":     data.get("error", "Exchange error"),
                "severity":  "error",
            },
            dedup_key="exchange_error",
        )

    def _on_system_alert(self, event: Event) -> None:
        data = event.data or {}
        self.notify(
            "system_alert", data,
            dedup_key=f"alert_{data.get('title','general')}",
        )

    def _on_candidate_approved(self, event: Event) -> None:
        """Scanner found a high-confidence trade candidate."""
        data = dict(event.data or {})
        sym  = data.get("symbol", "???")
        # Normalise score → confidence and models_fired → strategy/contributing_signals
        if "confidence" not in data and "score" in data:
            data["confidence"] = float(data["score"] or 0.0)
        if "strategy" not in data:
            mf = data.get("models_fired") or []
            if isinstance(mf, list) and mf:
                data["strategy"] = ", ".join(mf)
                data.setdefault("contributing_signals", mf)
            elif isinstance(mf, str) and mf:
                data["strategy"] = mf
        self.notify(
            "strategy_signal", data,
            dedup_key=f"candidate_{sym}_{data.get('direction','')}",
        )

    def _on_feed_status(self, event: Event) -> None:
        """Track data feed active/inactive state for health check reporting."""
        data = event.data or {}
        self._feed_active = bool(data.get("active", False))


# ── Module-level singleton ─────────────────────────────────────
notification_manager: NotificationManager = NotificationManager()
