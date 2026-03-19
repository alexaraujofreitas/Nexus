# ============================================================
# NEXUS TRADER — Exchange Management Page
# Full CCXT multi-exchange management with encrypted API keys
# ============================================================

import logging
import threading
from typing import Optional
from datetime import datetime

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QTableWidget, QTableWidgetItem, QLineEdit,
    QComboBox, QCheckBox, QDialog, QDialogButtonBox,
    QFormLayout, QMessageBox, QHeaderView, QSizePolicy,
    QGroupBox, QGridLayout, QTextEdit, QProgressBar,
    QTabWidget, QAbstractItemView
)
from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtGui import QColor, QFont

from gui.main_window import PageHeader
from config.constants import SUPPORTED_EXCHANGES
from core.event_bus import bus, Topics
from core.database.engine import get_session
from core.database.models import Exchange

logger = logging.getLogger(__name__)


# ── Exchange Connection Worker ────────────────────────────────
class ConnectionTestWorker(QThread):
    """Background thread to test exchange connectivity."""
    result = Signal(bool, str)  # success, message

    # Exchanges that support CCXT sandbox/testnet mode
    SANDBOX_SUPPORTED = {"binance", "bybit", "okx"}

    def __init__(self, exchange_id: str, api_key: str, api_secret: str,
                 passphrase: str = "", sandbox: bool = False, demo: bool = False):
        super().__init__()
        self.exchange_id = exchange_id
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.sandbox = sandbox
        self.demo = demo

    @staticmethod
    def _apply_bybit_demo(exchange_obj) -> None:
        """
        Switch Bybit to its Demo Trading environment (api-demo.bybit.com).

        CCXT stores URL templates as 'https://api.{hostname}' — NOT the resolved
        hostname — so simple string replacement never matches.  CCXT already
        provides a dedicated 'demotrading' key in urls; we just swap 'api' to it.
        Falls back to enable_demo_trading() if the method exists in newer builds.
        """
        try:
            # Preferred: CCXT built-in helper (available in some 4.x builds)
            if hasattr(exchange_obj, "enable_demo_trading"):
                exchange_obj.enable_demo_trading(True)
                return

            # Reliable fallback: replace the 'api' URL set with 'demotrading'
            demo_urls = exchange_obj.urls.get("demotrading")
            if demo_urls:
                exchange_obj.urls["api"] = demo_urls
        except Exception:
            pass

    def run(self):
        try:
            import ccxt
            exchange_class = getattr(ccxt, self.exchange_id)
            config = {
                "apiKey":          self.api_key,
                "secret":          self.api_secret,
                "enableRateLimit": True,
                "timeout":         10000,
            }
            if self.passphrase:
                config["password"] = self.passphrase

            # Only set sandbox for exchanges that actually support it
            # KuCoin, Coinbase, Kraken do NOT have CCXT sandbox URLs
            if self.sandbox and self.exchange_id in self.SANDBOX_SUPPORTED:
                config["sandbox"] = True

            exchange = exchange_class(config)

            # Demo Trading: redirect Bybit REST URLs before any network call
            if self.demo and self.exchange_id == "bybit":
                self._apply_bybit_demo(exchange)

            # Step 1: Test connectivity (no auth required)
            markets = exchange.load_markets()
            market_count = len(markets)

            # Step 2: Test API credentials if provided
            if self.api_key and self.api_secret:
                balance = exchange.fetch_balance()
                usdt = balance.get("USDT", {}).get("free", 0)
                mode_tag = " [DEMO]" if self.demo else (" [TESTNET]" if self.sandbox else "")
                self.result.emit(
                    True,
                    f"Connected{mode_tag} ✓  |  {market_count} markets  |  USDT balance: {usdt:.2f}"
                )
            else:
                self.result.emit(
                    True,
                    f"Connected ✓  |  {market_count} markets loaded  (no API keys tested)"
                )

        except Exception as e:
            self.result.emit(False, str(e))


# ── Crypto Helper ─────────────────────────────────────────────
def _encrypt(value: str) -> str:
    """Encrypt a string using Fernet symmetric encryption."""
    try:
        from cryptography.fernet import Fernet
        import os
        from pathlib import Path
        from config.constants import DATA_DIR

        key_file = DATA_DIR / ".nexus_key"
        if not key_file.exists():
            key = Fernet.generate_key()
            key_file.write_bytes(key)
            key_file.chmod(0o600)
        else:
            key = key_file.read_bytes()
        f = Fernet(key)
        return f.encrypt(value.encode()).decode()
    except Exception as e:
        logger.error("Encryption failed: %s", e)
        return value


def _decrypt(value: str) -> str:
    """Decrypt a Fernet-encrypted string."""
    try:
        from cryptography.fernet import Fernet
        from config.constants import DATA_DIR

        key_file = DATA_DIR / ".nexus_key"
        if not key_file.exists():
            return value
        key = key_file.read_bytes()
        f = Fernet(key)
        return f.decrypt(value.encode()).decode()
    except Exception:
        return value  # Return as-is if decryption fails (may be plain text)


# ── Add/Edit Exchange Dialog ──────────────────────────────────
class ExchangeDialog(QDialog):
    """Modal dialog for adding or editing an exchange configuration."""

    def __init__(self, parent=None, exchange: Optional[Exchange] = None):
        super().__init__(parent)
        self.setWindowTitle("Add Exchange" if exchange is None else "Edit Exchange")
        self.setMinimumWidth(520)
        self.setModal(True)
        self._exchange = exchange
        self._worker: Optional[ConnectionTestWorker] = None
        self._build()
        if exchange:
            self._populate(exchange)

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setContentsMargins(24, 24, 24, 24)

        # Title
        title = QLabel("Configure Exchange Connection")
        title.setStyleSheet("font-size: 15px; font-weight: 700; color: #E8EBF0;")
        layout.addWidget(title)

        # Form
        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignRight)

        # Exchange selector
        self.combo_exchange = QComboBox()
        for ex_id, ex_name in SUPPORTED_EXCHANGES.items():
            self.combo_exchange.addItem(ex_name, ex_id)
        form.addRow("Exchange:", self.combo_exchange)

        # Trading mode selector: Live / Sandbox / Demo
        self.combo_mode = QComboBox()
        self.combo_mode.addItem("🟢  Live  (real funds)",               "live")
        self.combo_mode.addItem("🟡  Sandbox / Testnet  (Binance, Bybit, OKX)", "sandbox")
        self.combo_mode.addItem("🔵  Demo Trading  (Bybit only)",       "demo")
        self.combo_mode.setCurrentIndex(0)
        self.combo_mode.currentIndexChanged.connect(self._on_mode_changed)
        self.combo_exchange.currentIndexChanged.connect(self._on_exchange_changed)
        form.addRow("Mode:", self.combo_mode)

        # API Key
        self.txt_api_key = QLineEdit()
        self.txt_api_key.setPlaceholderText("Paste your API Key here")
        form.addRow("API Key:", self.txt_api_key)

        # API Secret
        self.txt_api_secret = QLineEdit()
        self.txt_api_secret.setPlaceholderText("Paste your API Secret here")
        self.txt_api_secret.setEchoMode(QLineEdit.Password)
        form.addRow("API Secret:", self.txt_api_secret)

        # Passphrase (KuCoin, OKX)
        self.txt_passphrase = QLineEdit()
        self.txt_passphrase.setPlaceholderText("KuCoin/OKX only — leave blank for Binance/Bybit")
        self.txt_passphrase.setEchoMode(QLineEdit.Password)
        form.addRow("Passphrase:", self.txt_passphrase)

        layout.addLayout(form)

        # Security note
        note = QFrame()
        note.setObjectName("alert_info")
        note_layout = QHBoxLayout(note)
        note_layout.setContentsMargins(12, 10, 12, 10)
        self._note_lbl = QLabel(
            "🔒  API credentials are encrypted with AES-256 (Fernet) and stored locally.\n"
            "    They never leave your machine. Use read+trade permissions only — never withdrawal.\n"
            "    KuCoin note: sandbox mode is not supported — your real API keys connect to live markets."
        )
        note_lbl = self._note_lbl
        note_lbl.setWordWrap(True)
        note_lbl.setStyleSheet("color: #88AAFF; font-size: 13px;")
        note_layout.addWidget(note_lbl)
        layout.addWidget(note)

        # Test connection
        self.btn_test = QPushButton("⟳  Test Connection")
        self.btn_test.setObjectName("btn_ghost")
        self.btn_test.clicked.connect(self._test_connection)
        self.btn_test.setCursor(Qt.PointingHandCursor)

        self._test_result = QLabel("")
        self._test_result.setStyleSheet("font-size: 13px;")

        test_row = QHBoxLayout()
        test_row.addWidget(self.btn_test)
        test_row.addWidget(self._test_result)
        test_row.addStretch()
        layout.addLayout(test_row)

        # Buttons
        btn_box = QDialogButtonBox()
        self.btn_save = QPushButton("Save Exchange")
        self.btn_save.setObjectName("btn_primary")
        self.btn_save.setCursor(Qt.PointingHandCursor)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.setCursor(Qt.PointingHandCursor)
        btn_box.addButton(self.btn_save, QDialogButtonBox.AcceptRole)
        btn_box.addButton(btn_cancel, QDialogButtonBox.RejectRole)
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

    def _on_exchange_changed(self, _index: int):
        """
        When the exchange changes, update the Mode hint text.
        If Bybit is selected and the current mode is still 'Live', nudge the
        user by highlighting that Demo Trading is available.
        """
        exchange_id = self.combo_exchange.currentData()
        current_mode = self.combo_mode.currentData()
        if exchange_id == "bybit" and current_mode == "live":
            # Highlight the demo option label so the user notices it
            self.combo_mode.setStyleSheet(
                "QComboBox { border: 1px solid #1E90FF; }"
            )
        else:
            self.combo_mode.setStyleSheet("")
        # Re-run mode hint update with the current mode unchanged
        self._on_mode_changed(self.combo_mode.currentIndex())

    def _on_mode_changed(self, _index: int):
        """Update the hint text whenever the mode selector changes."""
        mode = self.combo_mode.currentData()
        # Clear the blue nudge highlight once the user makes any selection
        if mode != "live":
            self.combo_mode.setStyleSheet("")
        if mode == "demo":
            self._note_lbl.setText(
                "🔵  Demo Trading mode — uses api-demo.bybit.com.\n"
                "    Create Demo Trading API keys at demo.bybit.com (not the main site).\n"
                "    Paper money only — no real funds involved.  Bybit only."
            )
        elif mode == "sandbox":
            self._note_lbl.setText(
                "🟡  Sandbox / Testnet mode — uses exchange testnet endpoints.\n"
                "    Supported on Binance, Bybit, OKX only.  KuCoin has no sandbox.\n"
                "    Create testnet API keys from your exchange's developer portal."
            )
        else:
            self._note_lbl.setText(
                "🔒  API credentials are encrypted with AES-256 (Fernet) and stored locally.\n"
                "    They never leave your machine. Use read+trade permissions only — never withdrawal.\n"
                "    KuCoin note: sandbox mode is not supported — your real API keys connect to live markets."
            )

    def _populate(self, exchange: Exchange):
        """Fill form with existing exchange data (for editing)."""
        for i in range(self.combo_exchange.count()):
            if self.combo_exchange.itemData(i) == exchange.exchange_id:
                self.combo_exchange.setCurrentIndex(i)
                break
        # Restore mode selection
        demo_mode = getattr(exchange, "demo_mode", False)
        if demo_mode:
            mode_key = "demo"
        elif exchange.sandbox_mode:
            mode_key = "sandbox"
        else:
            mode_key = "live"
        for i in range(self.combo_mode.count()):
            if self.combo_mode.itemData(i) == mode_key:
                self.combo_mode.setCurrentIndex(i)
                break
        self.txt_api_key.setText(_decrypt(exchange.api_key_encrypted or ""))
        self.txt_api_secret.setText("••••••••" if exchange.api_secret_encrypted else "")
        self.txt_passphrase.setText("••••••••" if exchange.api_passphrase_encrypted else "")

    def _test_connection(self):
        if not self.txt_api_key.text() or not self.txt_api_secret.text():
            self._test_result.setText("⚠ Enter API Key and Secret first")
            self._test_result.setStyleSheet("color: #FFB300; font-size: 13px;")
            return

        self.btn_test.setEnabled(False)
        self._test_result.setText("⟳ Testing...")
        self._test_result.setStyleSheet("color: #8899AA; font-size: 13px;")

        exchange_id = self.combo_exchange.currentData()
        secret = self.txt_api_secret.text()
        if "•" in secret:
            secret = ""  # Don't re-send masked secret

        mode = self.combo_mode.currentData()
        self._worker = ConnectionTestWorker(
            exchange_id=exchange_id,
            api_key=self.txt_api_key.text(),
            api_secret=secret,
            passphrase=self.txt_passphrase.text(),
            sandbox=(mode == "sandbox"),
            demo=(mode == "demo"),
        )
        self._worker.result.connect(self._on_test_result)
        self._worker.start()

    def _on_test_result(self, success: bool, message: str):
        self.btn_test.setEnabled(True)
        if success:
            self._test_result.setText(f"✓  {message}")
            self._test_result.setStyleSheet("color: #00FF88; font-size: 13px;")
        else:
            short_msg = message[:80] + "..." if len(message) > 80 else message
            self._test_result.setText(f"✗  {short_msg}")
            self._test_result.setStyleSheet("color: #FF3355; font-size: 13px;")

    def get_data(self) -> dict:
        """Return form data as dict (ready to save)."""
        mode = self.combo_mode.currentData()
        return {
            "exchange_id": self.combo_exchange.currentData(),
            "name":        self.combo_exchange.currentText(),
            "api_key":     self.txt_api_key.text(),
            "api_secret":  self.txt_api_secret.text(),
            "passphrase":  self.txt_passphrase.text(),
            "sandbox":     (mode == "sandbox"),
            "demo":        (mode == "demo"),
        }


# ── Exchange Card Widget ──────────────────────────────────────
class ExchangeCard(QFrame):
    """Visual card for a single exchange configuration."""

    edit_requested = Signal(int)     # exchange db id
    delete_requested = Signal(int)
    activate_requested = Signal(int)

    def __init__(self, exchange: Exchange, parent=None):
        super().__init__(parent)
        self.exchange = exchange
        self.setObjectName("card")
        self.setMinimumHeight(120)
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)

        # Top row
        top = QHBoxLayout()
        name_lbl = QLabel(self.exchange.name)
        name_lbl.setStyleSheet("font-size: 15px; font-weight: 700; color: #E8EBF0;")
        top.addWidget(name_lbl)
        top.addStretch()

        # Status badge
        if self.exchange.is_active:
            badge = QLabel("● ACTIVE")
            badge.setStyleSheet("color: #00FF88; font-size: 13px; font-weight: 700;")
        else:
            badge = QLabel("● INACTIVE")
            badge.setStyleSheet("color: #4A5568; font-size: 13px; font-weight: 700;")
        top.addWidget(badge)

        # Mode badge — Live / Sandbox / Demo
        _demo    = getattr(self.exchange, "demo_mode", False)
        _sandbox = self.exchange.sandbox_mode
        if _demo:
            mode_text  = "DEMO"
            mode_color = "#1E90FF"
        elif _sandbox:
            mode_text  = "SANDBOX"
            mode_color = "#FFD700"
        else:
            mode_text  = "LIVE"
            mode_color = "#FF3355"
        mode_badge = QLabel(mode_text)
        mode_badge.setStyleSheet(
            f"color: {mode_color}; font-size: 13px; font-weight: 700; "
            f"border: 1px solid {mode_color}; padding: 2px 6px; border-radius: 3px;"
        )
        top.addWidget(mode_badge)
        layout.addLayout(top)

        # Mid row — key info
        mid = QHBoxLayout()
        exchange_id_lbl = QLabel(f"Exchange ID: {self.exchange.exchange_id}")
        exchange_id_lbl.setStyleSheet("color: #8899AA; font-size: 13px;")
        mid.addWidget(exchange_id_lbl)
        mid.addStretch()

        key_status = "🔑 API Keys Configured" if self.exchange.api_key_encrypted else "⚠ No API Keys"
        key_color = "#8899AA" if self.exchange.api_key_encrypted else "#FF6B00"
        key_lbl = QLabel(key_status)
        key_lbl.setStyleSheet(f"color: {key_color}; font-size: 13px;")
        mid.addWidget(key_lbl)
        layout.addLayout(mid)

        # Bottom row — actions
        bot = QHBoxLayout()
        bot.setSpacing(8)

        btn_edit = QPushButton("Edit")
        btn_edit.setObjectName("btn_ghost")
        btn_edit.setFixedHeight(28)
        btn_edit.setCursor(Qt.PointingHandCursor)
        btn_edit.clicked.connect(lambda: self.edit_requested.emit(self.exchange.id))

        btn_delete = QPushButton("Remove")
        btn_delete.setObjectName("btn_ghost")
        btn_delete.setFixedHeight(28)
        btn_delete.setCursor(Qt.PointingHandCursor)
        btn_delete.setStyleSheet("color: #FF3355; border-color: #FF3355;")
        btn_delete.clicked.connect(lambda: self.delete_requested.emit(self.exchange.id))

        if self.exchange.is_active:
            btn_toggle = QPushButton("Deactivate")
            btn_toggle.setStyleSheet("color: #FFB300;")
        else:
            btn_toggle = QPushButton("Set Active")
            btn_toggle.setObjectName("btn_success")

        btn_toggle.setFixedHeight(28)
        btn_toggle.setCursor(Qt.PointingHandCursor)
        btn_toggle.clicked.connect(lambda: self.activate_requested.emit(self.exchange.id))

        bot.addWidget(btn_edit)
        bot.addWidget(btn_delete)
        bot.addStretch()
        bot.addWidget(btn_toggle)
        layout.addLayout(bot)


# ── Exchange Management Page ──────────────────────────────────
class ExchangeManagementPage(QWidget):
    """Full exchange management page."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build()
        self._restore_from_vault()
        self._refresh()

    def _restore_from_vault(self):
        """
        If the database has no exchanges but the vault has backed-up credentials,
        restore them automatically. Protects against accidental database deletion.
        """
        try:
            from core.security.key_vault import key_vault
            from core.database.engine import get_session
            from core.database.models import Exchange

            with get_session() as session:
                count = session.query(Exchange).count()
                if count > 0:
                    return  # Database is intact, nothing to restore

            # Database is empty — check vault for backed-up exchange credentials
            known_exchanges = ["kucoin", "binance", "bybit", "okx", "coinbase",
                               "kraken", "bitget", "gateio"]
            restored = 0
            for eid in known_exchanges:
                api_key    = key_vault.load(f"exchange.{eid}.api_key")
                api_secret = key_vault.load(f"exchange.{eid}.api_secret")
                if not api_key or not api_secret:
                    continue
                passphrase = key_vault.load(f"exchange.{eid}.passphrase")
                data = {
                    "exchange_id": eid,
                    "name": eid.capitalize(),
                    "sandbox": False,
                    "api_key": api_key,
                    "api_secret": api_secret,
                    "passphrase": passphrase,
                }
                self._save_exchange(data, exchange_id=None)
                restored += 1
                logger.info("Restored exchange from vault: %s", eid)

            if restored:
                logger.info("Vault restore complete: %d exchange(s) recovered", restored)
        except Exception as exc:
            logger.debug("Vault restore skipped: %s", exc)

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header
        header = PageHeader(
            "Exchange Management",
            "Configure exchange connections and API credentials"
        )
        btn_add = QPushButton("＋  Add Exchange")
        btn_add.setObjectName("btn_primary")
        btn_add.setFixedHeight(36)
        btn_add.setCursor(Qt.PointingHandCursor)
        btn_add.clicked.connect(self._add_exchange)
        header.add_action(btn_add)
        layout.addWidget(header)

        # Tab widget
        self._tabs = QTabWidget()
        self._tabs.setStyleSheet(
            "QTabWidget::pane { border: none; background: #0A0E1A; }"
            "QTabBar::tab { background: #0F1623; color: #8899AA; padding: 8px 20px; "
            "border: 1px solid #1A2332; border-bottom: none; font-size: 13px; }"
            "QTabBar::tab:selected { background: #0A0E1A; color: #E8EBF0; border-top: 2px solid #1E90FF; }"
            "QTabBar::tab:hover { color: #E8EBF0; }"
        )

        # ── Tab 1: Exchanges ──────────────────────────────────
        exchange_tab = QWidget()
        content_layout = QHBoxLayout(exchange_tab)
        content_layout.setContentsMargins(24, 24, 24, 24)
        content_layout.setSpacing(24)

        # Left: Exchange Cards
        left = QVBoxLayout()
        left.setSpacing(12)

        sub_title = QLabel("CONFIGURED EXCHANGES")
        sub_title.setObjectName("card_title")
        left.addWidget(sub_title)

        self._cards_container = QVBoxLayout()
        self._cards_container.setSpacing(10)
        self._no_exchange_lbl = QLabel(
            "No exchanges configured yet.\nClick '+ Add Exchange' to get started."
        )
        self._no_exchange_lbl.setAlignment(Qt.AlignCenter)
        self._no_exchange_lbl.setStyleSheet("color: #4A5568; font-size: 14px; padding: 40px;")
        self._cards_container.addWidget(self._no_exchange_lbl)
        left.addLayout(self._cards_container)
        left.addStretch()

        # Right: Info Panel
        right = QVBoxLayout()
        right.setSpacing(12)

        info_title = QLabel("SUPPORTED EXCHANGES")
        info_title.setObjectName("card_title")
        right.addWidget(info_title)

        for ex_id, ex_name in SUPPORTED_EXCHANGES.items():
            row = QFrame()
            row.setObjectName("card")
            row.setFixedHeight(50)
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(12, 8, 12, 8)
            lbl_name = QLabel(ex_name)
            lbl_name.setStyleSheet("font-weight: 600; color: #E8EBF0;")
            lbl_id = QLabel(ex_id)
            lbl_id.setStyleSheet("color: #4A5568; font-size: 13px;")
            row_layout.addWidget(lbl_name)
            row_layout.addStretch()
            row_layout.addWidget(lbl_id)
            right.addWidget(row)

        right.addStretch()

        # Security card
        sec_card = QFrame()
        sec_card.setObjectName("card")
        sec_layout = QVBoxLayout(sec_card)
        sec_layout.setContentsMargins(14, 12, 14, 12)
        sec_layout.setSpacing(6)
        sec_title = QLabel("🔒 SECURITY")
        sec_title.setObjectName("card_title")
        sec_layout.addWidget(sec_title)
        for point in [
            "All API keys encrypted with AES-256 (Fernet)",
            "Keys stored locally — never transmitted",
            "Use Read + Trade permissions only",
            "Never grant Withdrawal permissions",
            "Enable IP whitelist on your exchange",
        ]:
            lbl = QLabel(f"  ✓  {point}")
            lbl.setStyleSheet("color: #8899AA; font-size: 13px;")
            sec_layout.addWidget(lbl)
        right.addWidget(sec_card)

        # Assemble exchange tab
        left_widget = QWidget()
        left_widget.setLayout(left)
        right_widget = QWidget()
        right_widget.setLayout(right)
        right_widget.setFixedWidth(320)

        content_layout.addWidget(left_widget, 1)
        content_layout.addWidget(right_widget)
        self._tabs.addTab(exchange_tab, "🔌  Exchanges")

        # ── Tab 2: Asset Management ───────────────────────────
        asset_tab = QWidget()
        asset_layout = QVBoxLayout(asset_tab)
        asset_layout.setContentsMargins(24, 16, 24, 24)
        asset_layout.setSpacing(12)

        # Toolbar
        asset_toolbar = QFrame()
        asset_toolbar.setObjectName("card")
        asset_toolbar.setFixedHeight(52)
        at = QHBoxLayout(asset_toolbar)
        at.setContentsMargins(12, 8, 12, 8)
        at.setSpacing(8)

        at.addWidget(QLabel("Quote:"))
        self._quote_filter = QComboBox()
        self._quote_filter.addItems(["USDT", "BTC", "ETH", "BNB"])
        self._quote_filter.setFixedWidth(80)
        at.addWidget(self._quote_filter)

        self._asset_search = QLineEdit()
        self._asset_search.setPlaceholderText("Search symbol…")
        self._asset_search.setFixedWidth(140)
        self._asset_search.textChanged.connect(self._filter_assets)
        at.addWidget(self._asset_search)

        at.addStretch()

        self._sync_btn = QPushButton("⟳  Sync Assets from Exchange")
        self._sync_btn.setObjectName("btn_primary")
        self._sync_btn.setFixedSize(200, 32)
        self._sync_btn.clicked.connect(self._sync_assets)
        at.addWidget(self._sync_btn)

        self._asset_status = QLabel("")
        self._asset_status.setStyleSheet("color: #5A7A9A; font-size: 13px;")
        at.addWidget(self._asset_status)

        asset_layout.addWidget(asset_toolbar)

        # Asset table
        self._asset_table = QTableWidget(0, 6)
        self._asset_table.setHorizontalHeaderLabels([
            "Symbol", "Base", "Quote", "Price Precision",
            "Min Amount", "Min Cost"
        ])
        self._asset_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for c in range(1, 6):
            self._asset_table.horizontalHeader().setSectionResizeMode(
                c, QHeaderView.ResizeToContents
            )
        self._asset_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._asset_table.setAlternatingRowColors(True)
        self._asset_table.verticalHeader().setVisible(False)
        self._asset_table.setSortingEnabled(True)
        self._asset_table.setStyleSheet(
            "QTableWidget { background: #0A0E1A; color: #E8EBF0; gridline-color: #1A2332; "
            "font-size: 13px; border: none; }"
            "QTableWidget::item:selected { background: #1A2D4A; }"
            "QTableWidget::item:alternate { background: #0D1220; }"
            "QHeaderView::section { background: #0F1623; color: #8899AA; padding: 6px; "
            "border: none; border-bottom: 1px solid #1A2332; font-size: 13px; }"
        )
        asset_layout.addWidget(self._asset_table, 1)

        # Asset count label
        self._asset_count_label = QLabel("0 assets loaded")
        self._asset_count_label.setStyleSheet("color: #5A7A9A; font-size: 13px;")
        asset_layout.addWidget(self._asset_count_label)

        self._tabs.addTab(asset_tab, "🪙  Asset Management")
        self._tabs.currentChanged.connect(self._on_tab_changed)

        layout.addWidget(self._tabs, 1)
        self._load_assets_from_db()

    def _refresh(self):
        """Reload exchange list from database."""
        # Clear existing cards
        while self._cards_container.count():
            item = self._cards_container.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        with get_session() as session:
            exchanges = session.query(Exchange).all()

            if not exchanges:
                self._no_exchange_lbl = QLabel(
                    "No exchanges configured yet.\nClick '+ Add Exchange' to get started."
                )
                self._no_exchange_lbl.setAlignment(Qt.AlignCenter)
                self._no_exchange_lbl.setStyleSheet("color: #4A5568; font-size: 14px; padding: 40px;")
                self._cards_container.addWidget(self._no_exchange_lbl)
                return

            for exchange in exchanges:
                # Detach from session for use in widget
                session.expunge(exchange)
                card = ExchangeCard(exchange)
                card.edit_requested.connect(self._edit_exchange)
                card.delete_requested.connect(self._delete_exchange)
                card.activate_requested.connect(self._toggle_active)
                self._cards_container.addWidget(card)

    def _add_exchange(self):
        dialog = ExchangeDialog(self)
        if dialog.exec() == QDialog.Accepted:
            data = dialog.get_data()
            self._save_exchange(data, exchange_id=None)

    def _edit_exchange(self, exchange_db_id: int):
        with get_session() as session:
            exchange = session.get(Exchange, exchange_db_id)
            if not exchange:
                return
            session.expunge(exchange)
        dialog = ExchangeDialog(self, exchange=exchange)
        if dialog.exec() == QDialog.Accepted:
            data = dialog.get_data()
            self._save_exchange(data, exchange_id=exchange_db_id)

    def _save_exchange(self, data: dict, exchange_id: Optional[int]):
        try:
            with get_session() as session:
                if exchange_id:
                    exchange = session.get(Exchange, exchange_id)
                    if not exchange:
                        return
                else:
                    exchange = Exchange()
                    session.add(exchange)

                exchange.exchange_id = data["exchange_id"]
                exchange.name = data["name"]
                exchange.sandbox_mode = data["sandbox"]
                exchange.demo_mode = data.get("demo", False)

                if data["api_key"]:
                    exchange.api_key_encrypted = _encrypt(data["api_key"])
                if data["api_secret"] and "•" not in data["api_secret"]:
                    exchange.api_secret_encrypted = _encrypt(data["api_secret"])
                if data["passphrase"] and "•" not in data["passphrase"]:
                    exchange.api_passphrase_encrypted = _encrypt(data["passphrase"])

                session.flush()

            # Backup credentials to vault so they survive database deletion
            try:
                from core.security.key_vault import key_vault
                eid = data["exchange_id"]
                if data["api_key"]:
                    key_vault.save(f"exchange.{eid}.api_key", data["api_key"])
                if data["api_secret"] and "•" not in data["api_secret"]:
                    key_vault.save(f"exchange.{eid}.api_secret", data["api_secret"])
                if data["passphrase"] and "•" not in data["passphrase"]:
                    key_vault.save(f"exchange.{eid}.passphrase", data["passphrase"])
                logger.info("Exchange credentials backed up to vault: %s", eid)
            except Exception as ve:
                logger.warning("Vault backup failed for exchange %s: %s", data.get("exchange_id"), ve)

            logger.info("Exchange saved: %s", data["name"])
            self._refresh()

        except Exception as e:
            logger.error("Failed to save exchange: %s", e)
            QMessageBox.critical(self, "Error", f"Failed to save exchange:\n{e}")

    def _delete_exchange(self, exchange_db_id: int):
        reply = QMessageBox.question(
            self, "Remove Exchange",
            "Are you sure you want to remove this exchange?\n"
            "This will not affect existing trade history.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            with get_session() as session:
                exchange = session.get(Exchange, exchange_db_id)
                if exchange:
                    session.delete(exchange)
            self._refresh()

    def _toggle_active(self, exchange_db_id: int):
        with get_session() as session:
            # Deactivate all, activate only the chosen one
            for ex in session.query(Exchange).all():
                ex.is_active = (ex.id == exchange_db_id and not ex.is_active)

        self._refresh()

        # Reload the singleton exchange_manager so the whole app picks it up
        try:
            from core.market_data.exchange_manager import exchange_manager
            import threading
            threading.Thread(
                target=exchange_manager.load_active_exchange,
                daemon=True
            ).start()
        except Exception as e:
            logger.warning("Could not reload exchange_manager: %s", e)

        # Read the newly-activated exchange's mode so the sidebar label updates
        _mode = "live"
        try:
            with get_session() as _s:
                _active = _s.query(Exchange).filter_by(is_active=True).first()
                if _active:
                    _mode = getattr(_active, "mode", "live")
        except Exception:
            pass

        bus.publish(Topics.EXCHANGE_CONNECTED,
                    {"name": "Exchange", "connected": True,
                     "exchange_mode": _mode},
                    source="exchange_management")

    def _on_tab_changed(self, index: int):
        if index == 1:   # Asset Management tab
            self._load_assets_from_db()

    # ── Asset Management ───────────────────────────────────────
    def _sync_assets(self):
        """Sync all active spot symbols from exchange to DB."""
        try:
            from core.market_data.exchange_manager import exchange_manager
            if not exchange_manager.is_connected():
                # Try to load
                exchange_manager.load_active_exchange()
            if not exchange_manager.is_connected():
                self._asset_status.setText("⚠ Not connected — activate an exchange first")
                return

            with get_session() as session:
                exch = session.query(Exchange).filter_by(is_active=True).first()
                if not exch:
                    self._asset_status.setText("⚠ No active exchange")
                    return
                exch_id = exch.id

            self._sync_btn.setEnabled(False)
            self._asset_status.setText("Syncing…")
            added = exchange_manager.sync_assets_to_db(exch_id)
            self._asset_status.setText(f"✓ {added} new assets synced")
            self._load_assets_from_db()

        except Exception as e:
            self._asset_status.setText(f"⚠ Error: {e}")
            logger.error("Asset sync error: %s", e)
        finally:
            self._sync_btn.setEnabled(True)

    def _load_assets_from_db(self):
        """Load assets from DB into the table."""
        try:
            from core.database.models import Asset
            quote = self._quote_filter.currentText() if hasattr(self, "_quote_filter") else "USDT"
            with get_session() as session:
                rows = [
                    {
                        "symbol":   a.symbol,
                        "base":     a.base_currency or "",
                        "quote":    a.quote_currency or "",
                        "price_p":  str(a.price_precision or ""),
                        "min_amt":  f"{a.min_amount:.8f}" if a.min_amount else "—",
                        "min_cost": f"{a.min_cost:.4f}" if a.min_cost else "—",
                    }
                    for a in session.query(Asset)
                    .filter_by(quote_currency=quote)
                    .order_by(Asset.symbol)
                    .all()
                ]
            self._all_assets = rows
            self._render_asset_table(rows)
        except Exception as e:
            logger.warning("Load assets failed: %s", e)

    def _filter_assets(self, text: str):
        if not hasattr(self, "_all_assets"):
            return
        filtered = [r for r in self._all_assets
                    if text.upper() in r["symbol"].upper()]
        self._render_asset_table(filtered)

    def _render_asset_table(self, rows: list):
        self._asset_table.setSortingEnabled(False)
        self._asset_table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            def ci(t, color="#E8EBF0"):
                item = QTableWidgetItem(t)
                item.setForeground(QColor(color))
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                return item
            self._asset_table.setItem(i, 0, ci(r["symbol"]))
            self._asset_table.setItem(i, 1, ci(r["base"],  "#8899AA"))
            self._asset_table.setItem(i, 2, ci(r["quote"], "#8899AA"))
            self._asset_table.setItem(i, 3, ci(r["price_p"], "#8899AA"))
            self._asset_table.setItem(i, 4, ci(r["min_amt"], "#8899AA"))
            self._asset_table.setItem(i, 5, ci(r["min_cost"], "#8899AA"))
        self._asset_table.setSortingEnabled(True)
        self._asset_count_label.setText(f"{len(rows)} assets")
