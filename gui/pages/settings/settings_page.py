# ============================================================
# NEXUS TRADER — Settings Page
# ============================================================

import logging
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QTabWidget, QFormLayout, QLineEdit, QDoubleSpinBox,
    QSpinBox, QCheckBox, QComboBox, QGroupBox, QScrollArea,
    QMessageBox,
)
from PySide6.QtCore import Qt

from gui.main_window import PageHeader
from config.settings import settings
from core.event_bus import bus, Topics

logger = logging.getLogger(__name__)

# Keys routed to the encrypted vault (never stored as plain text in YAML)
_VAULT_KEYS = {
    "ai.anthropic_api_key",
    "ai.openai_api_key",
    "ai.gemini_api_key",
    "sentiment.news_api_key",
    "sentiment.reddit_client_id",
    "sentiment.reddit_client_secret",
    "agents.fred_api_key",
    "agents.lunarcrush_api_key",
    "agents.coinglass_api_key",
    "agents.cryptopanic_api_key",
    # Notification channel secrets
    "notifications.twilio_sid",
    "notifications.twilio_token",
    "notifications.telegram_token",
    "notifications.email_password",
    "notifications.gemini_password",   # Gmail App Password for Gemini channel
}


def _vault_load(key: str) -> str:
    """Load a key from the vault; return '' if not set."""
    try:
        from core.security.key_vault import key_vault
        return key_vault.load(key)
    except Exception:
        return ""


def _vault_save(key: str, value: str):
    """Save a key to the vault."""
    try:
        from core.security.key_vault import key_vault
        key_vault.save(key, value)
    except Exception as exc:
        logger.error("KeyVault save failed for '%s': %s", key, exc)


class SettingsSection(QGroupBox):
    """A labeled settings group with form fields."""
    def __init__(self, title: str, parent=None):
        super().__init__(title, parent)
        self._form = QFormLayout()
        self._form.setSpacing(10)
        self._form.setLabelAlignment(Qt.AlignRight)
        self.setLayout(self._form)
        self._fields: dict[str, QWidget] = {}

    def add_double(self, key: str, label: str, value: float,
                   min_val: float = 0.0, max_val: float = 100.0,
                   decimals: int = 2, suffix: str = "") -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setMinimum(min_val)
        spin.setMaximum(max_val)
        spin.setDecimals(decimals)
        spin.setValue(value)
        if suffix:
            spin.setSuffix(f" {suffix}")
        self._form.addRow(label, spin)
        self._fields[key] = spin
        return spin

    def add_text(self, key: str, label: str, value: str,
                 password: bool = False, placeholder: str = "") -> QLineEdit:
        edit = QLineEdit(value)
        if password:
            edit.setEchoMode(QLineEdit.Password)
        if placeholder:
            edit.setPlaceholderText(placeholder)
        self._form.addRow(label, edit)
        self._fields[key] = edit
        return edit

    def add_combo(self, key: str, label: str, options: list[str],
                  current: str) -> QComboBox:
        combo = QComboBox()
        for opt in options:
            combo.addItem(opt)
        if current in options:
            combo.setCurrentText(current)
        self._form.addRow(label, combo)
        self._fields[key] = combo
        return combo

    def add_int(self, key: str, label: str, value: int,
                min_val: int = 0, max_val: int = 100,
                suffix: str = "") -> QSpinBox:
        spin = QSpinBox()
        spin.setMinimum(min_val)
        spin.setMaximum(max_val)
        spin.setValue(int(value))
        if suffix:
            spin.setSuffix(f" {suffix}")
        self._form.addRow(label, spin)
        self._fields[key] = spin
        return spin

    def add_check(self, key: str, label: str, checked: bool) -> QCheckBox:
        chk = QCheckBox()
        chk.setChecked(checked)
        self._form.addRow(label, chk)
        self._fields[key] = chk
        return chk

    def get_values(self) -> dict:
        result = {}
        for key, widget in self._fields.items():
            if isinstance(widget, (QDoubleSpinBox, QSpinBox)):
                result[key] = widget.value()
            elif isinstance(widget, QLineEdit):
                result[key] = widget.text()
            elif isinstance(widget, QComboBox):
                result[key] = widget.currentText()
            elif isinstance(widget, QCheckBox):
                result[key] = widget.isChecked()
        return result


class SettingsPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = PageHeader("Settings", "Configure platform behavior, risk parameters, and AI thresholds")
        btn_save = QPushButton("💾  Save All Settings")
        btn_save.setObjectName("btn_primary")
        btn_save.setFixedHeight(36)
        btn_save.setCursor(Qt.PointingHandCursor)
        btn_save.clicked.connect(self._save_all)
        header.add_action(btn_save)
        layout.addWidget(header)

        # Tab widget
        tabs = QTabWidget()
        tabs.setContentsMargins(24, 24, 24, 24)

        tabs.addTab(self._build_risk_tab(), "⊘  Risk Management")
        tabs.addTab(self._build_ai_tab(), "◈  AI & ML")
        tabs.addTab(self._build_data_tab(), "◎  Data & Feeds")
        tabs.addTab(self._build_backtest_tab(), "⊟  Backtesting")
        tabs.addTab(self._build_notifications_tab(), "⊕  Notifications")
        tabs.addTab(self._build_agents_tab(), "◉  Intelligence Agents")
        tabs.addTab(self._build_portfolio_tab(), "◑  Portfolio Allocation")

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(24, 24, 24, 24)
        content_layout.addWidget(tabs)
        layout.addWidget(content, 1)

    def _build_risk_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        container = QWidget()
        v = QVBoxLayout(container)
        v.setSpacing(16)
        v.setContentsMargins(0, 8, 0, 8)

        self._risk_section = SettingsSection("Position & Portfolio Risk")
        self._risk_section.add_double("risk.max_position_pct", "Max Position Size:",
            settings.get("risk.max_position_pct", 2.0), 0.1, 50.0, 1, "%")
        self._risk_section.add_double("risk.max_portfolio_drawdown_pct", "Max Portfolio Drawdown:",
            settings.get("risk.max_portfolio_drawdown_pct", 15.0), 1.0, 100.0, 1, "%")
        self._risk_section.add_double("risk.max_strategy_drawdown_pct", "Max Strategy Drawdown:",
            settings.get("risk.max_strategy_drawdown_pct", 10.0), 1.0, 100.0, 1, "%")
        self._risk_section.add_double("risk.min_sharpe_live", "Min Sharpe (Live):",
            settings.get("risk.min_sharpe_live", 0.5), 0.0, 5.0, 2)
        self._risk_section.add_double("risk.max_spread_pct", "Max Spread Filter:",
            settings.get("risk.max_spread_pct", 0.3), 0.01, 5.0, 2, "%")
        self._risk_section.add_double("risk.default_stop_loss_pct", "Default Stop Loss:",
            settings.get("risk.default_stop_loss_pct", 2.0), 0.1, 50.0, 1, "%")
        self._risk_section.add_double("risk.default_take_profit_pct", "Default Take Profit:",
            settings.get("risk.default_take_profit_pct", 4.0), 0.1, 100.0, 1, "%")
        v.addWidget(self._risk_section)

        self._idss_section = SettingsSection("IDSS Scanner — RiskGate & Confluence")
        self._idss_section.setToolTip(
            "These parameters are applied to the live scanner immediately on save — "
            "no restart required."
        )
        self._idss_section.add_int("risk.max_concurrent_positions", "Max Concurrent Positions:",
            int(settings.get("risk.max_concurrent_positions", 3)), 1, 20, "positions")
        self._idss_section.add_double("risk.min_risk_reward", "Min Risk:Reward Ratio:",
            settings.get("risk.min_risk_reward", 1.3), 0.5, 10.0, 2)
        self._idss_section.add_double("idss.min_confluence_score", "Min Confluence Score:",
            settings.get("idss.min_confluence_score", 0.55), 0.10, 0.99, 2)
        # Small hint label
        hint = QLabel(
            "ℹ  Changes take effect immediately when you click Save — "
            "the scanner does not need to be restarted."
        )
        hint.setStyleSheet("color:#445566; font-size:13px; padding:4px 8px;")
        hint.setWordWrap(True)
        self._idss_section.layout().addRow("", hint)
        v.addWidget(self._idss_section)

        v.addStretch()
        scroll.setWidget(container)
        return scroll

    def _build_ai_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        container = QWidget()
        v = QVBoxLayout(container)
        v.setSpacing(16)
        v.setContentsMargins(0, 8, 0, 8)

        self._ai_section = SettingsSection("AI & Language Models")
        self._ai_section.add_combo("ai.active_provider", "Active AI Provider:",
            ["Auto (Anthropic → OpenAI → Gemini)", "Anthropic Claude", "OpenAI",
             "Google Gemini", "Local (Ollama)"],
            settings.get("ai.active_provider", "Auto (Anthropic → OpenAI → Gemini)"))
        self._ai_section.add_text("ai.anthropic_api_key", "Anthropic API Key:",
            _vault_load("ai.anthropic_api_key"),
            password=True, placeholder="sk-ant-... (Claude)  🔒 stored encrypted")
        self._ai_section.add_combo("ai.anthropic_model", "Anthropic Model:",
            ["claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
            settings.get("ai.anthropic_model", "claude-opus-4-6"))
        self._ai_section.add_text("ai.openai_api_key", "OpenAI API Key:",
            _vault_load("ai.openai_api_key"),
            password=True, placeholder="sk-... (OpenAI)  🔒 stored encrypted")
        self._ai_section.add_combo("ai.openai_model", "OpenAI Model:",
            ["gpt-4o", "gpt-4-turbo", "gpt-4", "gpt-3.5-turbo"],
            settings.get("ai.openai_model", "gpt-4o"))
        self._ai_section.add_text("ai.gemini_api_key", "Google Gemini API Key:",
            _vault_load("ai.gemini_api_key"),
            password=True, placeholder="AIza... (Google Gemini)  🔒 stored encrypted")
        self._ai_section.add_combo("ai.gemini_model", "Gemini Model:",
            ["gemini-2.0-flash", "gemini-2.5-pro-exp-03-25",
             "gemini-1.5-pro", "gemini-1.5-flash"],
            settings.get("ai.gemini_model", "gemini-2.0-flash"))
        self._ai_section.add_combo("ai.ollama_model", "Ollama Model:",
            ["deepseek-r1:14b", "deepseek-r1:7b", "qwen2.5:14b",
             "qwen2.5:7b", "llama3.1:8b", "mistral:7b", "phi4:14b"],
            settings.get("ai.ollama_model", "deepseek-r1:14b"))
        self._ai_section.add_text("ai.ollama_url", "Ollama URL:",
            settings.get("ai.ollama_url", "http://localhost:11434/v1"),
            placeholder="http://localhost:11434/v1")
        self._ai_section.add_double("ai.ml_confidence_threshold", "ML Confidence Threshold:",
            settings.get("ai.ml_confidence_threshold", 0.65), 0.5, 0.99, 2)
        self._ai_section.add_double("ai.retrain_interval_hours", "Model Retrain Interval:",
            settings.get("ai.retrain_interval_hours", 24.0), 1.0, 168.0, 0, "hours")
        self._ai_section.add_check("ai.strategy_generation_enabled", "Enable AI Strategy Generation:",
            settings.get("ai.strategy_generation_enabled", True))
        v.addWidget(self._ai_section)
        v.addStretch()
        scroll.setWidget(container)
        return scroll

    def _build_data_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        container = QWidget()
        v = QVBoxLayout(container)
        v.setSpacing(16)
        v.setContentsMargins(0, 8, 0, 8)

        self._data_section = SettingsSection("Data & Market Feeds")
        self._data_section.add_combo("data.default_timeframe", "Default Timeframe:",
            ["1m", "5m", "15m", "1h", "4h", "1d"],
            settings.get("data.default_timeframe", "1h"))
        self._data_section.add_double("data.historical_days", "Historical Data (Days):",
            settings.get("data.historical_days", 365), 30, 1825, 0, "days")
        self._data_section.add_check("data.cache_enabled", "Enable Data Cache:",
            settings.get("data.cache_enabled", True))
        v.addWidget(self._data_section)

        self._sent_section = SettingsSection("Sentiment Data Sources")
        self._sent_section.add_check("sentiment.news_enabled", "Crypto News API:",
            settings.get("sentiment.news_enabled", True))
        self._sent_section.add_text("sentiment.news_api_key", "News API Key:",
            _vault_load("sentiment.news_api_key"),
            password=True, placeholder="NewsAPI.org key (newsapi.org)  \U0001f512 stored encrypted")
        self._sent_section.add_text("agents.cryptopanic_api_key", "CryptoPanic API Key:",
            _vault_load("agents.cryptopanic_api_key"),
            password=True, placeholder="CryptoPanic key (cryptopanic.com)  🔒 stored encrypted")
        self._sent_section.add_check("sentiment.reddit_enabled", "Reddit Sentiment:",
            settings.get("sentiment.reddit_enabled", False))
        self._sent_section.add_text("sentiment.reddit_client_id", "Reddit Client ID:",
            _vault_load("sentiment.reddit_client_id"),
            placeholder="Reddit app client_id  🔒 stored encrypted")
        self._sent_section.add_text("sentiment.reddit_client_secret", "Reddit Secret:",
            _vault_load("sentiment.reddit_client_secret"),
            password=True, placeholder="Reddit app client_secret  🔒 stored encrypted")
        v.addWidget(self._sent_section)
        v.addStretch()
        scroll.setWidget(container)
        return scroll

    def _build_backtest_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        container = QWidget()
        v = QVBoxLayout(container)
        v.setSpacing(16)
        v.setContentsMargins(0, 8, 0, 8)

        self._bt_section = SettingsSection("Backtesting Defaults")
        self._bt_section.add_double("backtesting.default_fee_pct", "Default Trading Fee:",
            settings.get("backtesting.default_fee_pct", 0.1), 0.0, 2.0, 3, "%")
        self._bt_section.add_double("backtesting.default_slippage_pct", "Default Slippage:",
            settings.get("backtesting.default_slippage_pct", 0.05), 0.0, 1.0, 3, "%")
        self._bt_section.add_double("backtesting.default_initial_capital", "Default Capital:",
            settings.get("backtesting.default_initial_capital", 10000.0), 100.0, 10_000_000.0, 2, "USDT")
        self._bt_section.add_double("backtesting.walk_forward_train_months", "WF Train Window:",
            settings.get("backtesting.walk_forward_train_months", 24), 6, 60, 0, "months")
        self._bt_section.add_double("backtesting.walk_forward_validate_months", "WF Validate Window:",
            settings.get("backtesting.walk_forward_validate_months", 6), 1, 24, 0, "months")
        v.addWidget(self._bt_section)
        v.addStretch()
        scroll.setWidget(container)
        return scroll

    def _build_notifications_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        container = QWidget()
        v = QVBoxLayout(container)
        v.setSpacing(16)
        v.setContentsMargins(0, 8, 0, 8)

        # ── WhatsApp (Primary Channel) ─────────────────────────
        self._wa_section = SettingsSection("WhatsApp Notifications  (Primary — Twilio)")
        self._wa_section.add_check(
            "notifications.whatsapp.enabled", "Enable WhatsApp:",
            settings.get("notifications.whatsapp.enabled", False),
        )
        self._wa_section.add_text(
            "notifications.twilio_sid", "Twilio Account SID:",
            _vault_load("notifications.twilio_sid"),
            password=True, placeholder="ACxxxxxxxxxxxxxxxx",
        )
        self._wa_section.add_text(
            "notifications.twilio_token", "Twilio Auth Token:",
            _vault_load("notifications.twilio_token"),
            password=True, placeholder="Your Twilio Auth Token",
        )
        self._wa_section.add_text(
            "notifications.whatsapp.from_number", "WhatsApp From (Twilio):",
            settings.get("notifications.whatsapp.from_number", ""),
            placeholder="whatsapp:+14155238886",
        )
        self._wa_section.add_text(
            "notifications.whatsapp.to_number", "Your WhatsApp Number:",
            settings.get("notifications.whatsapp.to_number", ""),
            placeholder="whatsapp:+15551234567",
        )

        # Test button
        wa_test_btn = QPushButton("Test WhatsApp")
        wa_test_btn.setObjectName("secondary_btn")
        wa_test_btn.clicked.connect(self._test_whatsapp)
        self._wa_section.layout().addRow("", wa_test_btn)
        v.addWidget(self._wa_section)

        # ── Telegram (Secondary) ──────────────────────────────
        self._tg_section = SettingsSection("Telegram Notifications  (Secondary)")
        self._tg_section.add_check(
            "notifications.telegram.enabled", "Enable Telegram:",
            settings.get("notifications.telegram.enabled", False),
        )
        self._tg_section.add_text(
            "notifications.telegram_token", "Bot Token:",
            _vault_load("notifications.telegram_token"),
            password=True, placeholder="From @BotFather",
        )
        self._tg_section.add_text(
            "notifications.telegram.chat_id", "Chat ID:",
            settings.get("notifications.telegram.chat_id", ""),
            placeholder="Numeric ID or @channel_name",
        )

        tg_test_btn = QPushButton("Test Telegram")
        tg_test_btn.setObjectName("secondary_btn")
        tg_test_btn.clicked.connect(self._test_telegram)
        self._tg_section.layout().addRow("", tg_test_btn)
        v.addWidget(self._tg_section)

        # ── Email ─────────────────────────────────────────────
        self._email_section = SettingsSection("Email Notifications")
        self._email_section.add_check(
            "notifications.email.enabled", "Enable Email:",
            settings.get("notifications.email.enabled", False),
        )
        self._email_section.add_text(
            "notifications.email.smtp_host", "SMTP Host:",
            settings.get("notifications.email.smtp_host", "smtp.gmail.com"),
            placeholder="smtp.gmail.com",
        )
        self._email_section.add_text(
            "notifications.email.smtp_port", "SMTP Port:",
            str(settings.get("notifications.email.smtp_port", 587)),
            placeholder="587",
        )
        self._email_section.add_text(
            "notifications.email.username", "SMTP Username:",
            settings.get("notifications.email.username", ""),
            placeholder="your@gmail.com",
        )
        self._email_section.add_text(
            "notifications.email_password", "App Password:",
            _vault_load("notifications.email_password"),
            password=True, placeholder="Gmail App Password (not your main password)",
        )
        self._email_section.add_text(
            "notifications.email.from_address", "From Address:",
            settings.get("notifications.email.from_address", ""),
            placeholder="nexustrader@gmail.com",
        )
        self._email_section.add_text(
            "notifications.email.to_addresses", "To Address(es):",
            settings.get("notifications.email.to_addresses", ""),
            placeholder="you@example.com (comma-separate multiple)",
        )

        em_test_btn = QPushButton("Test Email")
        em_test_btn.setObjectName("secondary_btn")
        em_test_btn.clicked.connect(self._test_email)
        self._email_section.layout().addRow("", em_test_btn)
        v.addWidget(self._email_section)

        # ── Gemini (Google Account) ────────────────────────────
        self._gemini_section = SettingsSection(
            "Gemini / Google Account Notifications"
        )
        gemini_hint = QLabel(
            "ℹ  Delivers alerts to your Gmail inbox via your Google account.\n"
            "   Requires a Gmail App Password (not your main password).\n"
            "   Enable 2-Step Verification → App Passwords at myaccount.google.com."
        )
        gemini_hint.setStyleSheet("color:#4285F4; font-size:13px; padding:4px 8px;")
        gemini_hint.setWordWrap(True)
        self._gemini_section.layout().addRow("", gemini_hint)
        self._gemini_section.add_check(
            "notifications.gemini.enabled", "Enable Gemini Channel:",
            settings.get("notifications.gemini.enabled", False),
        )
        self._gemini_section.add_text(
            "notifications.gemini.username", "Gmail Address:",
            settings.get("notifications.gemini.username", ""),
            placeholder="yourname@gmail.com",
        )
        self._gemini_section.add_text(
            "notifications.gemini_password", "Gmail App Password:",
            _vault_load("notifications.gemini_password"),
            password=True,
            placeholder="16-char App Password from myaccount.google.com  🔒 encrypted",
        )
        self._gemini_section.add_text(
            "notifications.gemini.to_address", "Deliver To (Gmail):",
            settings.get("notifications.gemini.to_address", ""),
            placeholder="Same as Gmail Address (or another Gmail)",
        )
        self._gemini_section.add_check(
            "notifications.gemini.ai_enrich", "AI-Enrich Notifications:",
            settings.get("notifications.gemini.ai_enrich", False),
        )
        ai_enrich_hint = QLabel(
            "When enabled, Gemini Flash adds a 2–3 sentence analysis to each alert\n"
            "using your Gemini API key from the AI & ML tab."
        )
        ai_enrich_hint.setStyleSheet("color:#445566; font-size:13px; padding:2px 8px;")
        ai_enrich_hint.setWordWrap(True)
        self._gemini_section.layout().addRow("", ai_enrich_hint)

        gm_test_btn = QPushButton("Test Gemini")
        gm_test_btn.setObjectName("secondary_btn")
        gm_test_btn.clicked.connect(self._test_gemini)
        self._gemini_section.layout().addRow("", gm_test_btn)
        v.addWidget(self._gemini_section)

        # ── SMS ───────────────────────────────────────────────
        self._sms_section = SettingsSection("SMS Notifications  (uses same Twilio account)")
        self._sms_section.add_check(
            "notifications.sms.enabled", "Enable SMS:",
            settings.get("notifications.sms.enabled", False),
        )
        self._sms_section.add_text(
            "notifications.sms.from_number", "SMS From (Twilio number):",
            settings.get("notifications.sms.from_number", ""),
            placeholder="+14155238886  (plain E.164 — no 'whatsapp:' prefix)",
        )
        self._sms_section.add_text(
            "notifications.sms.to_number", "Your Phone Number:",
            settings.get("notifications.sms.to_number", ""),
            placeholder="+15551234567",
        )
        v.addWidget(self._sms_section)

        # ── Notification Preferences ──────────────────────────
        self._notif_pref_section = SettingsSection("What to Notify")
        _pref_items = [
            ("trade_opened",     "Trade Opened",          True),
            ("trade_closed",     "Trade Closed",          True),
            ("trade_stopped",    "Stop-Loss Hit",         True),
            ("trade_rejected",   "Signal Rejected",       False),
            ("trade_modified",   "Trade Modified",        False),
            ("strategy_signal",  "Strategy Signal Alert", False),
            ("risk_warning",     "Risk Warning",          True),
            ("market_condition", "Regime / Market Alert", False),
            ("system_error",     "System Errors",         True),
            ("emergency_stop",   "Emergency Stop",        True),
            ("daily_summary",    "Daily Summary",         True),
        ]
        for key, label, default in _pref_items:
            self._notif_pref_section.add_check(
                f"notifications.preferences.{key}", f"{label}:",
                settings.get(f"notifications.preferences.{key}", default),
            )
        v.addWidget(self._notif_pref_section)

        # ── Legacy desktop prefs (kept for compatibility) ─────
        self._notif_section = SettingsSection("Desktop Notifications")
        self._notif_section.add_check("notifications.desktop_enabled", "Desktop Notifications:",
            settings.get("notifications.desktop_enabled", True))
        v.addWidget(self._notif_section)

        v.addStretch()
        scroll.setWidget(container)
        return scroll

    def _test_whatsapp(self) -> None:
        """Test WhatsApp channel using current form values (no save required)."""
        try:
            from core.notifications.channels.whatsapp_channel import WhatsAppChannel
            vals = self._wa_section.get_values()
            cfg = {
                "enabled":      True,
                "account_sid":  vals.get("notifications.twilio_sid",
                                         _vault_load("notifications.twilio_sid")),
                "auth_token":   vals.get("notifications.twilio_token",
                                         _vault_load("notifications.twilio_token")),
                "from_number":  vals.get("notifications.whatsapp.from_number", ""),
                "to_number":    vals.get("notifications.whatsapp.to_number", ""),
            }
            ch = WhatsAppChannel(cfg)
            if not ch.is_configured:
                QMessageBox.warning(
                    self, "WhatsApp Test",
                    "WhatsApp channel not configured.\n\n"
                    "Please fill in: Twilio Account SID, Twilio Auth Token, "
                    "From Number and To Number."
                )
                return
            ok = ch.test()
            if ok:
                QMessageBox.information(self, "WhatsApp Test",
                                        "✅ Test message sent successfully.")
            else:
                QMessageBox.critical(self, "WhatsApp Test",
                                     "❌ Send failed — check credentials and channel status.")
        except Exception as exc:
            QMessageBox.critical(self, "WhatsApp Test", f"Error: {exc}")

    def _test_telegram(self) -> None:
        """Test Telegram channel using current form values (no save required)."""
        try:
            from core.notifications.channels.telegram_channel import TelegramChannel
            vals = self._tg_section.get_values()
            cfg = {
                "enabled":   True,
                "bot_token": vals.get("notifications.telegram_token",
                                      _vault_load("notifications.telegram_token")),
                "chat_id":   vals.get("notifications.telegram.chat_id", ""),
            }
            ch = TelegramChannel(cfg)
            if not ch.is_configured:
                QMessageBox.warning(
                    self, "Telegram Test",
                    "Telegram channel not configured.\n\n"
                    "Please fill in: Bot Token and Chat ID."
                )
                return
            ok = ch.test()
            if ok:
                QMessageBox.information(self, "Telegram Test",
                                        "✅ Test message sent successfully.")
            else:
                QMessageBox.critical(self, "Telegram Test",
                                     "❌ Send failed — check bot token and chat ID.")
        except Exception as exc:
            QMessageBox.critical(self, "Telegram Test", f"Error: {exc}")

    def _test_email(self) -> None:
        """Test Email channel using current form values (no save required)."""
        try:
            from core.notifications.channels.email_channel import EmailChannel
            vals = self._email_section.get_values()
            to_raw = vals.get("notifications.email.to_addresses", "")
            to_list = [a.strip() for a in to_raw.split(",") if a.strip()]
            cfg = {
                "enabled":      True,
                "smtp_host":    vals.get("notifications.email.smtp_host",
                                         "smtp.gmail.com"),
                "smtp_port":    int(vals.get("notifications.email.smtp_port",
                                             587) or 587),
                "username":     vals.get("notifications.email.username", ""),
                "password":     vals.get("notifications.email_password",
                                         _vault_load("notifications.email_password")),
                "from_address": vals.get("notifications.email.from_address", ""),
                "to_addresses": to_list,
                "use_tls":      True,
            }
            ch = EmailChannel(cfg)
            if not ch.is_configured:
                QMessageBox.warning(
                    self, "Email Test",
                    "Email channel not configured.\n\n"
                    "Please fill in: SMTP Host, SMTP Username, App Password, "
                    "From Address, and To Address."
                )
                return
            ok = ch.test()
            if ok:
                QMessageBox.information(self, "Email Test",
                                        "✅ Test email sent successfully.")
            else:
                QMessageBox.critical(self, "Email Test",
                                     "❌ Send failed — check SMTP credentials.\n\n"
                                     "For Gmail: make sure 2FA is enabled and you "
                                     "are using an App Password, not your main password.")
        except Exception as exc:
            QMessageBox.critical(self, "Email Test", f"Error: {exc}")

    def _test_gemini(self) -> None:
        """Test Gemini channel using current form values (no save required)."""
        try:
            from core.notifications.channels.gemini_channel import GeminiChannel
            vals = self._gemini_section.get_values()
            # Pull Gemini API key from AI section if available (for AI enrichment)
            try:
                ai_vals = self._ai_section.get_values()
                gemini_api_key = (
                    ai_vals.get("ai.gemini_api_key")
                    or _vault_load("ai.gemini_api_key")
                )
            except Exception:
                gemini_api_key = _vault_load("ai.gemini_api_key")

            cfg = {
                "enabled":        True,
                "smtp_host":      "smtp.gmail.com",
                "smtp_port":      587,
                "username":       vals.get("notifications.gemini.username", ""),
                "password":       vals.get("notifications.gemini_password",
                                           _vault_load("notifications.gemini_password")),
                "from_address":   vals.get("notifications.gemini.username", ""),
                "to_address":     vals.get("notifications.gemini.to_address", "")
                                  or vals.get("notifications.gemini.username", ""),
                "use_tls":        True,
                "ai_enrich":      vals.get("notifications.gemini.ai_enrich", False),
                "gemini_api_key": gemini_api_key,
            }
            ch = GeminiChannel(cfg)
            if not ch.is_configured:
                QMessageBox.warning(
                    self, "Gemini Test",
                    "Gemini channel not configured.\n\n"
                    "Please fill in: Gmail Address and Gmail App Password.\n\n"
                    "To create an App Password:\n"
                    "  1. Go to myaccount.google.com\n"
                    "  2. Security → 2-Step Verification → App Passwords\n"
                    "  3. Create password named 'NexusTrader'"
                )
                return
            ok = ch.test()
            if ok:
                QMessageBox.information(
                    self, "Gemini Test",
                    "✅ Test notification sent to your Gmail inbox!\n\n"
                    "Check your Google account inbox for the test message."
                )
            else:
                QMessageBox.critical(
                    self, "Gemini Test",
                    "❌ Send failed — check your Gmail credentials.\n\n"
                    "Make sure you are using a Gmail App Password\n"
                    "(not your regular Google account password)."
                )
        except Exception as exc:
            QMessageBox.critical(self, "Gemini Test", f"Error: {exc}")

    def _build_agents_tab(self) -> QWidget:
        """Intelligence agents configuration tab."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        container = QWidget()
        v = QVBoxLayout(container)
        v.setSpacing(16)
        v.setContentsMargins(0, 8, 0, 8)

        # ── General agent settings ─────────────────────────────
        self._agents_section = SettingsSection("Agent Behaviour")
        self._agents_section.add_check(
            "agents.auto_start", "Auto-start agents on exchange connect:",
            settings.get("agents.auto_start", True),
        )
        self._agents_section.add_double(
            "agents.min_confluence_boost", "Agent confluence boost threshold:",
            settings.get("agents.min_confluence_boost", 0.25),
            min_val=0.0, max_val=1.0, decimals=2,
        )
        v.addWidget(self._agents_section)

        # ── Liquidation Intelligence ───────────────────────────
        self._liq_section = SettingsSection("Liquidation Intelligence Agent")
        self._liq_section.add_text(
            "agents.coinglass_api_key", "Coinglass API Key:",
            _vault_load("agents.coinglass_api_key"),
            password=True,
            placeholder="Optional — coinglass.com (free tier available)  🔒 stored encrypted",
        )
        v.addWidget(self._liq_section)

        # ── MacroAgent ────────────────────────────────────────
        self._macro_section = SettingsSection("Macro Intelligence Agent")
        self._macro_section.add_text(
            "agents.fred_api_key", "FRED API Key:",
            _vault_load("agents.fred_api_key"),
            password=True,
            placeholder="Optional — free at fred.stlouisfed.org",
        )
        v.addWidget(self._macro_section)

        # ── Social Sentiment Agent ────────────────────────────
        self._social_section = SettingsSection("Social Sentiment Agent")
        self._social_section.add_text(
            "agents.lunarcrush_api_key", "LunarCrush API Key:",
            _vault_load("agents.lunarcrush_api_key"),
            password=True,
            placeholder="Optional — free tier available at lunarcrush.com",
        )
        v.addWidget(self._social_section)

        # ── Options Flow Agent ────────────────────────────────
        self._options_section = SettingsSection("Options Flow Agent (BTC/ETH only)")
        self._options_section.add_check(
            "agents.options_enabled", "Enable Deribit options data:",
            settings.get("agents.options_enabled", True),
        )
        self._options_section.add_double(
            "agents.options_max_days_expiry", "Max days to expiry:",
            settings.get("agents.options_max_days_expiry", 35.0),
            min_val=7, max_val=90, decimals=0, suffix="days",
        )
        v.addWidget(self._options_section)

        # ── Funding Rate Agent ────────────────────────────────
        self._funding_section = SettingsSection("Funding Rate & Order Book Agents")
        self._funding_section.add_check(
            "agents.funding_enabled", "Enable funding rate monitoring:",
            settings.get("agents.funding_enabled", True),
        )
        self._funding_section.add_check(
            "agents.orderbook_enabled", "Enable order book monitoring:",
            settings.get("agents.orderbook_enabled", True),
        )
        v.addWidget(self._funding_section)

        v.addStretch()
        scroll.setWidget(container)
        return scroll

    # ── Portfolio Allocation tab ──────────────────────────────────────────────

    def _build_portfolio_tab(self) -> QWidget:
        """
        Portfolio Allocation Settings tab.

        Lets the user configure:
          - Mode: STATIC (fixed weights) or DYNAMIC (BTC-dominance-driven)
          - Static weights per symbol
          - BTC dominance value + thresholds (DYNAMIC mode)
          - Three regime profiles: BTC Dominant / Neutral / Alt Season
        """
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        container = QWidget()
        v = QVBoxLayout(container)
        v.setSpacing(16)
        v.setContentsMargins(0, 8, 0, 8)

        # ── Overview label ─────────────────────────────────────────────────
        info = QLabel(
            "<b>Symbol Priority & Allocation</b><br>"
            "Weights adjust candidate ranking only — they never modify signals, "
            "position sizing, stop-loss/take-profit, or any risk parameter.<br>"
            "<i>adjusted_score = base_score × symbol_weight</i><br>"
            "Higher-weight symbols are evaluated first when multiple IDSS "
            "candidates exist in the same scan cycle.<br>"
            "<br>"
            "<b>Study 4 Baseline:</b> SOL=1.3 (highest profit) · ETH=1.2 (highest quality) · "
            "BTC=1.0 (benchmark) · BNB=0.8 · XRP=0.8"
        )
        info.setWordWrap(True)
        info.setStyleSheet(
            "background:#1a2a3a; color:#aabbcc; padding:12px 16px; "
            "border-radius:6px; font-size:13px; border:1px solid #2a3a4a;"
        )
        v.addWidget(info)

        # ── Mode ───────────────────────────────────────────────────────────
        self._alloc_mode_section = SettingsSection("Allocation Mode")
        self._alloc_mode_section.add_combo(
            "symbol_allocation.mode",
            "Mode:",
            ["STATIC", "DYNAMIC"],
            settings.get("symbol_allocation.mode", "STATIC"),
        )
        mode_hint = QLabel(
            "STATIC — use fixed weights below regardless of market conditions.<br>"
            "DYNAMIC — automatically switch between three profiles based on "
            "BTC Dominance percentage."
        )
        mode_hint.setWordWrap(True)
        mode_hint.setStyleSheet("color:#778899; font-size:12px; padding:2px 4px;")
        self._alloc_mode_section.layout().addRow("", mode_hint)
        v.addWidget(self._alloc_mode_section)

        # ── Static weights ─────────────────────────────────────────────────
        _symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"]
        _static_defaults = {"BTC/USDT": 1.0, "ETH/USDT": 1.2, "SOL/USDT": 1.3,
                            "BNB/USDT": 0.8, "XRP/USDT": 0.8}

        self._alloc_static_section = SettingsSection(
            "Static Weights  (used in STATIC mode)"
        )
        for sym in _symbols:
            key = f"symbol_allocation.static_weights.{sym}"
            default = _static_defaults.get(sym, 1.0)
            self._alloc_static_section.add_double(
                key,
                f"{sym}:",
                settings.get(key, default),
                min_val=0.1, max_val=3.0, decimals=2,
            )
        static_hint = QLabel(
            "Range 0.10 – 3.00.  Values outside this range are automatically clamped."
        )
        static_hint.setStyleSheet("color:#778899; font-size:12px; padding:2px 4px;")
        self._alloc_static_section.layout().addRow("", static_hint)
        v.addWidget(self._alloc_static_section)

        # ── BTC Dominance (DYNAMIC mode) ───────────────────────────────────
        self._alloc_btc_dom_section = SettingsSection(
            "BTC Dominance  (DYNAMIC mode)"
        )
        self._alloc_btc_dom_section.add_double(
            "symbol_allocation.btc_dominance_pct",
            "Current BTC Dominance %:",
            settings.get("symbol_allocation.btc_dominance_pct", 50.0),
            min_val=0.0, max_val=100.0, decimals=1, suffix="%",
        )
        self._alloc_btc_dom_section.add_double(
            "symbol_allocation.btc_dominance_high",
            "High Threshold (→ BTC_DOMINANT):",
            settings.get("symbol_allocation.btc_dominance_high", 55.0),
            min_val=0.0, max_val=100.0, decimals=1, suffix="%",
        )
        self._alloc_btc_dom_section.add_double(
            "symbol_allocation.btc_dominance_low",
            "Low Threshold (→ ALT_SEASON):",
            settings.get("symbol_allocation.btc_dominance_low", 45.0),
            min_val=0.0, max_val=100.0, decimals=1, suffix="%",
        )
        dom_hint = QLabel(
            "Dominance > High → <b>BTC_DOMINANT</b> profile (favour BTC/ETH)<br>"
            "Dominance < Low → <b>ALT_SEASON</b> profile (favour SOL/alts)<br>"
            "Between thresholds → <b>NEUTRAL</b> profile (balanced weights)"
        )
        dom_hint.setWordWrap(True)
        dom_hint.setStyleSheet("color:#778899; font-size:12px; padding:2px 4px;")
        self._alloc_btc_dom_section.layout().addRow("", dom_hint)
        v.addWidget(self._alloc_btc_dom_section)

        # ── Regime profiles ────────────────────────────────────────────────
        _profile_defs = [
            (
                "btc_dominant",
                "BTC Dominant Profile  (dominance > High threshold)",
                {"BTC/USDT": 1.4, "ETH/USDT": 1.1, "SOL/USDT": 0.9,
                 "BNB/USDT": 0.7, "XRP/USDT": 0.7},
            ),
            (
                "neutral",
                "Neutral Profile  (between thresholds)",
                {"BTC/USDT": 1.0, "ETH/USDT": 1.2, "SOL/USDT": 1.3,
                 "BNB/USDT": 0.8, "XRP/USDT": 0.8},
            ),
            (
                "alt_season",
                "Alt Season Profile  (dominance < Low threshold)",
                {"BTC/USDT": 0.7, "ETH/USDT": 1.2, "SOL/USDT": 1.5,
                 "BNB/USDT": 1.0, "XRP/USDT": 1.0},
            ),
        ]

        self._alloc_profile_sections: dict[str, SettingsSection] = {}
        for profile_key, profile_title, profile_defaults in _profile_defs:
            sec = SettingsSection(profile_title)
            for sym in _symbols:
                key = f"symbol_allocation.profiles.{profile_key}.{sym}"
                default = profile_defaults.get(sym, 1.0)
                sec.add_double(
                    key,
                    f"{sym}:",
                    settings.get(key, default),
                    min_val=0.1, max_val=3.0, decimals=2,
                )
            self._alloc_profile_sections[profile_key] = sec
            v.addWidget(sec)

        v.addStretch()
        scroll.setWidget(container)
        return scroll

    def _save_all(self):
        try:
            changed: dict = {}
            vault_saved: list[str] = []

            for section in [
                self._risk_section,
                self._idss_section,
                self._ai_section,
                self._data_section,
                self._sent_section,
                self._bt_section,
                self._wa_section,
                self._tg_section,
                self._email_section,
                self._gemini_section,
                self._sms_section,
                self._notif_pref_section,
                self._notif_section,
                self._agents_section,
                self._liq_section,
                self._macro_section,
                self._social_section,
                self._options_section,
                self._funding_section,
                # Portfolio Allocation tab sections
                self._alloc_mode_section,
                self._alloc_static_section,
                self._alloc_btc_dom_section,
                *self._alloc_profile_sections.values(),
            ]:
                for key, value in section.get_values().items():
                    if key in _VAULT_KEYS:
                        # API keys → encrypted vault (never written to YAML)
                        # Don't re-save if the user didn't change the value
                        # (the field is pre-filled from vault; unchanged = same)
                        str_val = str(value).strip()
                        _vault_save(key, str_val)
                        vault_saved.append(key)
                        # Mark as vault-managed in YAML so llm_provider knows
                        settings.set(key, "__vault__")
                    else:
                        settings.set(key, value)
                        changed[key] = value

            # Notify all live components to reload their parameters
            bus.publish(Topics.SETTINGS_CHANGED, changed, source="settings_page")

            msg = "All settings saved and applied to the live scanner."
            if vault_saved:
                msg += f"\n\n🔒 {len(vault_saved)} API key(s) stored encrypted in the secure vault."
            QMessageBox.information(self, "Settings Saved", msg)
            logger.info(
                "Settings saved — %d YAML keys, %d vault keys",
                len(changed), len(vault_saved),
            )
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save settings:\n{e}")
            logger.error("Settings save failed: %s", e)
