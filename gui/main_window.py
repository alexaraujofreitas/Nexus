# ============================================================
# NEXUS TRADER — Main Window
# Bloomberg-style dark desktop application with sidebar nav
# ============================================================

import logging
from datetime import datetime
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QLabel, QPushButton, QStackedWidget, QFrame,
    QStatusBar, QSizePolicy, QSpacerItem, QScrollArea
)
from PySide6.QtCore import Qt, QTimer, Signal, QSize, Slot
from PySide6.QtGui import QFont, QIcon, QColor

from core.event_bus import bus, Topics

logger = logging.getLogger(__name__)

# ── Nav Item Definition ───────────────────────────────────────
NAV_ITEMS = [
    # (page_key, label, icon_char, section)
    # ── DASHBOARDS — overview & monitoring ────────────────────
    ("dashboard",            "Dashboard",             "◈", "DASHBOARDS"),
    ("demo_monitor",         "Demo Live Monitor",     "⊛", None),
    ("system_health",        "System Health",         "◎", None),
    ("risk_management",      "Risk Management",       "⊘", None),
    ("intelligence",         "AI Intelligence",       "◉", None),
    ("intelligence_agents",  "Intelligence Agents",   "◈", None),
    ("regime",               "Market Regime",         "⊘", None),
    # ── TRADING — active execution ────────────────────────────
    ("market_scanner",       "IDSS AI Scanner",       "⊡", "TRADING"),
    ("chart_workspace",      "Chart Workspace",       "⋈", None),
    ("paper_trading",        "Paper Trading",         "◎", None),
    # ── RESEARCH — pre/post trade analysis ────────────────────
    ("research_lab",         "Research Lab",          "⚗", "RESEARCH"),
    ("backtesting",          "Backtesting",           "⊟", None),
    ("news_sentiment",       "News & Sentiment",      "⊠", None),
    # ── ANALYSIS — post-trade review ──────────────────────────
    ("orders_positions",     "Orders & Positions",    "⊕", "ANALYSIS"),
    ("proposals",            "Tuning Proposals",      "⊙", None),
    ("performance_analytics","Performance Analytics", "◈", None),
    # ── SYSTEM — infrastructure & configuration ───────────────
    ("notifications",        "Notifications",         "⊕", "SYSTEM"),
    ("exchange_management",  "Exchange Management",   "⊞", None),
    ("settings",             "Settings",              "⊙", None),
    # ── always last ───────────────────────────────────────────
    ("logs",                 "Logs",                  "≡", None),
    ("help_center",          "Help Center",           "?", None),
]

# Pages not shown in sidebar but still navigable via go_to_page() for internal use
_HIDDEN_PAGES = {"strategies", "signal_explorer", "quant_dashboard"}


class SidebarButton(QPushButton):
    """A navigation button for the sidebar."""

    def __init__(self, page_key: str, label: str, icon_char: str):
        super().__init__()
        self.page_key = page_key
        self.setObjectName("nav_btn")
        self.setText(f"  {icon_char}  {label}")
        self.setCheckable(True)
        self.setMinimumHeight(40)
        self.setCursor(Qt.PointingHandCursor)


class Sidebar(QFrame):
    """Left-side navigation panel."""

    page_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("sidebar")
        self.setFixedWidth(220)
        self._buttons: dict[str, SidebarButton] = {}
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Logo ─────────────────────────────────────────────
        logo_frame = QFrame()
        logo_frame.setObjectName("sidebar_logo")
        logo_frame.setFixedHeight(64)
        logo_layout = QVBoxLayout(logo_frame)
        logo_layout.setContentsMargins(16, 10, 16, 10)
        logo_layout.setSpacing(2)

        logo_text = QLabel("NEXUS")
        logo_text.setObjectName("logo_text")
        font = QFont()
        font.setPointSize(16)
        font.setBold(True)
        logo_text.setFont(font)

        logo_sub = QLabel("TRADER  ▸  INSTITUTIONAL")
        logo_sub.setObjectName("logo_sub")
        font2 = QFont()
        font2.setPointSize(7)
        logo_sub.setFont(font2)

        logo_layout.addWidget(logo_text)
        logo_layout.addWidget(logo_sub)
        layout.addWidget(logo_frame)

        # ── Nav Items (inside a scroll area so they never overlap) ───
        nav_container = QWidget()
        nav_layout = QVBoxLayout(nav_container)
        nav_layout.setContentsMargins(0, 8, 0, 8)
        nav_layout.setSpacing(0)

        prev_section = None
        for page_key, label, icon_char, section in NAV_ITEMS:
            if section and section != prev_section:
                sec_label = QLabel(section)
                sec_label.setObjectName("nav_section")
                nav_layout.addWidget(sec_label)
                prev_section = section

            btn = SidebarButton(page_key, label, icon_char)
            btn.clicked.connect(lambda checked, k=page_key: self._on_nav_click(k))
            self._buttons[page_key] = btn
            nav_layout.addWidget(btn)

        nav_layout.addStretch()

        nav_scroll = QScrollArea()
        nav_scroll.setWidget(nav_container)
        nav_scroll.setWidgetResizable(True)
        nav_scroll.setFrameShape(QFrame.NoFrame)
        nav_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        nav_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        nav_scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollBar:vertical { background: #0F1623; width: 4px; border-radius: 2px; }"
            "QScrollBar::handle:vertical { background: #2A3A52; border-radius: 2px; min-height: 20px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }"
        )
        layout.addWidget(nav_scroll, 1)

        # ── Bottom Status ──────────────────────────────────────
        bottom = QFrame()
        bottom.setFixedHeight(50)
        bottom_layout = QHBoxLayout(bottom)
        bottom_layout.setContentsMargins(16, 8, 16, 8)
        self._mode_label = QLabel("● NO EXCHANGE")
        self._mode_label.setStyleSheet("color: #4A5568; font-size: 13px; font-weight: 700;")
        bottom_layout.addWidget(self._mode_label)
        layout.addWidget(bottom)

    def _on_nav_click(self, page_key: str):
        # Uncheck all buttons first
        for btn in self._buttons.values():
            btn.setChecked(False)
        self._buttons[page_key].setChecked(True)
        self.page_requested.emit(page_key)

    def set_active(self, page_key: str):
        for btn in self._buttons.values():
            btn.setChecked(False)
        if page_key in self._buttons:
            self._buttons[page_key].setChecked(True)

    def set_trading_mode(self, mode: str):
        """
        Update the bottom-left exchange mode badge.
        Accepts exchange-mode strings from EXCHANGE_CONNECTED events:
          live     → red  ● LIVE — REAL FUNDS
          sandbox  → gold ● SANDBOX / TESTNET
          demo     → blue ● DEMO TRADING
        Legacy paper/shadow keys kept for backward compatibility.
        """
        labels = {
            "live":    ("● LIVE — REAL FUNDS",   "#FF3355"),
            "sandbox": ("● SANDBOX / TESTNET",   "#FFD700"),
            "demo":    ("● DEMO TRADING",         "#1E90FF"),
            # Legacy
            "paper":   ("● PAPER MODE",           "#FFD700"),
            "shadow":  ("● SHADOW MODE",          "#88AAFF"),
            "off":     ("● NO EXCHANGE",          "#4A5568"),
        }
        text, color = labels.get(mode, ("● NO EXCHANGE", "#4A5568"))
        self._mode_label.setText(text)
        self._mode_label.setStyleSheet(f"color: {color}; font-size: 13px; font-weight: 700;")


class PageHeader(QFrame):
    """Standard header bar for each page."""

    def __init__(self, title: str, subtitle: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("page_header")
        self.setFixedHeight(56)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(24, 0, 24, 0)

        title_col = QVBoxLayout()
        title_col.setSpacing(2)

        lbl_title = QLabel(title)
        lbl_title.setObjectName("page_title")
        title_col.addWidget(lbl_title)

        if subtitle:
            lbl_sub = QLabel(subtitle)
            lbl_sub.setObjectName("page_subtitle")
            title_col.addWidget(lbl_sub)

        layout.addLayout(title_col)
        layout.addStretch()

        self._right_widgets = QHBoxLayout()
        self._right_widgets.setSpacing(8)
        layout.addLayout(self._right_widgets)

    def add_action(self, widget):
        self._right_widgets.addWidget(widget)


class NexusStatusBar(QStatusBar):
    """Custom status bar with connection indicators."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizeGripEnabled(False)

        # Clock
        self._clock = QLabel()
        self._clock.setStyleSheet("color: #4A5568; font-size: 13px; padding: 0 12px;")
        self.addPermanentWidget(self._clock)

        # Execution mode indicator (F-06)
        self._mode_label = QLabel("PAPER MODE")
        self._mode_label.setStyleSheet(
            "color: #00FF88; font-size: 13px; font-weight: bold; padding: 0 12px; "
            "background-color: rgba(0,255,136,0.15); border-radius: 4px;"
        )
        self.addPermanentWidget(self._mode_label)

        # Exchange status — name updated at runtime by MainWindow._restore_status_bar_exchange()
        self._exchange_status = QLabel("⬤ Exchange: Disconnected")
        self._exchange_status.setObjectName("status_disconnected")
        self._exchange_status.setStyleSheet("color: #FF3355; font-size: 13px; padding: 0 12px;")
        self.addPermanentWidget(self._exchange_status)

        # Data feed status
        self._feed_status = QLabel("⬤ Feed: Inactive")
        self._feed_status.setStyleSheet("color: #4A5568; font-size: 13px; padding: 0 12px;")
        self.addPermanentWidget(self._feed_status)

        # Model status
        self._model_status = QLabel("⬤ AI: Offline")
        self._model_status.setStyleSheet("color: #4A5568; font-size: 13px; padding: 0 12px;")
        self.addPermanentWidget(self._model_status)

        # Clock timer
        self._timer = QTimer()
        self._timer.timeout.connect(self._update_clock)
        self._timer.start(1000)
        self._update_clock()

    def _update_clock(self):
        now = datetime.utcnow().strftime("UTC  %Y-%m-%d  %H:%M:%S")
        self._clock.setText(now)

    def set_exchange_connected(self, name: str, connected: bool):
        if connected:
            self._exchange_status.setText(f"⬤ {name}: Connected")
            self._exchange_status.setStyleSheet("color: #00FF88; font-size: 13px; padding: 0 12px;")
        else:
            self._exchange_status.setText(f"⬤ {name}: Disconnected")
            self._exchange_status.setStyleSheet("color: #FF3355; font-size: 13px; padding: 0 12px;")

    def set_exchange_error(self, name: str, reason: str):
        """Show a specific error reason instead of the generic 'Disconnected'."""
        self._exchange_status.setText(f"⬤ {name}: {reason}")
        self._exchange_status.setStyleSheet("color: #FF8800; font-size: 13px; padding: 0 12px;")

    def set_feed_active(self, active: bool):
        if active:
            self._feed_status.setText("⬤ Feed: Active")
            self._feed_status.setStyleSheet("color: #00FF88; font-size: 13px; padding: 0 12px;")
        else:
            self._feed_status.setText("⬤ Feed: Inactive")
            self._feed_status.setStyleSheet("color: #4A5568; font-size: 13px; padding: 0 12px;")

    def set_model_active(self, active: bool):
        if active:
            self._model_status.setText("⬤ AI: Online")
            self._model_status.setStyleSheet("color: #1E90FF; font-size: 13px; padding: 0 12px;")
        else:
            self._model_status.setText("⬤ AI: Offline")
            self._model_status.setStyleSheet("color: #4A5568; font-size: 13px; padding: 0 12px;")

    def set_execution_mode(self, mode: str):
        """Update the execution mode indicator (F-06)."""
        if mode == "live":
            self._mode_label.setText("LIVE MODE")
            self._mode_label.setStyleSheet(
                "color: #FF3355; font-size: 13px; font-weight: bold; padding: 0 12px; "
                "background-color: rgba(255,51,85,0.15); border-radius: 4px;"
            )
        else:
            self._mode_label.setText("PAPER MODE")
            self._mode_label.setStyleSheet(
                "color: #00FF88; font-size: 13px; font-weight: bold; padding: 0 12px; "
                "background-color: rgba(0,255,136,0.15); border-radius: 4px;"
            )


class MainWindow(QMainWindow):
    """
    Root application window.
    Layout: Sidebar | Page Stack
    """

    # Thread-safe signals — emitted from event bus callbacks (any thread)
    # and connected to UI-update slots (main thread)
    _sig_exchange_connected = Signal(str, bool)         # name, connected
    _sig_exchange_error     = Signal(str, str)          # name, reason
    _sig_feed_status        = Signal(bool)              # active
    _sig_exchange_mode      = Signal(str)               # exchange mode string
    _sig_mode_changed       = Signal(str)               # execution mode: "paper" | "live"
    _sig_ai_status          = Signal(bool)              # AI online/offline

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Nexus Trader — Institutional Trading Platform")
        self.setMinimumSize(1440, 900)
        self.resize(1600, 960)
        self._pages: dict[str, QWidget] = {}
        self._build_ui()
        self._connect_signals()
        self._connect_events()
        self._navigate_to("dashboard")
        self._restore_exchange_mode()
        self._restore_status_bar_exchange()
        self._restore_execution_mode()
        self._start_ai_health_check()
        logger.info("MainWindow initialized")

    def _build_ui(self):
        # Central widget
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QHBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # Sidebar
        self.sidebar = Sidebar()
        self.sidebar.page_requested.connect(self._navigate_to)
        root_layout.addWidget(self.sidebar)

        # Page stack
        self.stack = QStackedWidget()
        self.stack.setObjectName("page_container")
        root_layout.addWidget(self.stack, 1)

        # Status bar
        self.status_bar = NexusStatusBar()
        self.setStatusBar(self.status_bar)

        # Load all pages (lazy import to keep startup fast)
        self._load_pages()

    def _load_pages(self):
        """Import and register all page widgets."""
        from gui.pages.dashboard.dashboard_page          import DashboardPage
        from gui.pages.market_scanner.scanner_page        import MarketScannerPage
        from gui.pages.chart_workspace.chart_page         import ChartWorkspacePage
        from gui.pages.strategies.strategies_page         import StrategiesPage
        from gui.pages.backtesting.backtesting_page       import BacktestingPage
        from gui.pages.paper_trading.paper_trading_page   import PaperTradingPage
        from gui.pages.news_sentiment.news_sentiment_page import NewsSentimentPage
        from gui.pages.risk_management.risk_page          import RiskManagementPage
        from gui.pages.orders_positions.orders_page       import OrdersPositionsPage
        from gui.pages.performance_analytics.analytics_page import PerformanceAnalyticsPage
        from gui.pages.proposals.proposals_page            import ProposalsPage
        from gui.pages.logs.logs_page                     import LogsPage
        from gui.pages.settings.settings_page             import SettingsPage
        from gui.pages.exchange_management.exchange_page  import ExchangeManagementPage
        # Phase 12 — new pages
        from gui.pages.intelligence.intelligence_page     import IntelligencePage
        from gui.pages.intelligence.agents_page           import IntelligenceAgentsPage
        from gui.pages.regime.regime_page                 import RegimePage
        from gui.pages.signal_explorer.signal_explorer_page import SignalExplorerPage
        from gui.pages.system_health.system_health_page   import SystemHealthPage
        from gui.pages.help.help_center_page              import HelpCenterPage
        from gui.pages.notifications.notifications_page   import NotificationsPage
        from gui.pages.demo_monitor.demo_monitor_page     import DemoMonitorPage
        from gui.pages.research_lab.research_lab_page    import ResearchLabPage
        page_map = {
            "research_lab":          ResearchLabPage,
            "dashboard":             DashboardPage,
            "demo_monitor":          DemoMonitorPage,
            "market_scanner":        MarketScannerPage,
            "chart_workspace":       ChartWorkspacePage,
            "strategies":            StrategiesPage,
            "backtesting":           BacktestingPage,
            "paper_trading":         PaperTradingPage,
            "signal_explorer":       SignalExplorerPage,
            "news_sentiment":        NewsSentimentPage,
            "intelligence":          IntelligencePage,
            "intelligence_agents":   IntelligenceAgentsPage,
            "regime":                RegimePage,
            "risk_management":       RiskManagementPage,
            "orders_positions":      OrdersPositionsPage,
            "proposals":             ProposalsPage,
            "performance_analytics": PerformanceAnalyticsPage,
            "notifications":         NotificationsPage,
            "system_health":         SystemHealthPage,
            "logs":                  LogsPage,
            "help_center":           HelpCenterPage,
            "settings":              SettingsPage,
            "exchange_management":   ExchangeManagementPage,
        }

        for key, PageClass in page_map.items():
            try:
                if PageClass is None:
                    raise ImportError(f"Page module for '{key}' failed to import")
                page = PageClass()
                self._pages[key] = page
                self.stack.addWidget(page)
            except Exception as e:
                logger.error("Failed to load page '%s': %s", key, e, exc_info=True)
                # Add placeholder on error
                placeholder = self._make_placeholder(key, str(e))
                self._pages[key] = placeholder
                self.stack.addWidget(placeholder)

    def _make_placeholder(self, page_key: str, error: str = "") -> QWidget:
        """Fallback placeholder widget for pages that fail to load."""
        w = QWidget()
        w._is_placeholder = True  # marker for lazy-loading detection
        lay = QVBoxLayout(w)
        lay.setAlignment(Qt.AlignCenter)
        msg = f"⚠ Page '{page_key}' failed to load\n{error}" if error else f"Loading '{page_key}'..."
        lbl = QLabel(msg)
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setStyleSheet("color: #FF3355; font-size: 14px;" if error else "color: #8899AA; font-size: 14px;")
        lay.addWidget(lbl)
        return w

    def _connect_signals(self):
        """Connect internal Qt signals to UI-update slots (always on main thread)."""
        self._sig_exchange_connected.connect(self._update_exchange_status)
        self._sig_exchange_error.connect(self._update_exchange_error)   # (str, str)
        self._sig_feed_status.connect(self._update_feed_status)
        self._sig_exchange_mode.connect(self.sidebar.set_trading_mode)
        self._sig_mode_changed.connect(self.status_bar.set_execution_mode)
        self._sig_ai_status.connect(self.status_bar.set_model_active)

    def _connect_events(self):
        """Subscribe to relevant EventBus topics (called from any thread)."""
        bus.subscribe(Topics.EXCHANGE_CONNECTED, self._on_exchange_connected)
        bus.subscribe(Topics.EXCHANGE_ERROR, self._on_exchange_error)
        bus.subscribe(Topics.FEED_STATUS, self._on_feed_status)
        bus.subscribe(Topics.MODE_CHANGED, self._on_mode_changed)

    def _restore_exchange_mode(self):
        """
        On startup, read the active exchange from the DB and set the sidebar
        mode label immediately — before any EXCHANGE_CONNECTED event fires.
        """
        try:
            from core.database.engine import get_session
            from core.database.models import Exchange
            with get_session() as session:
                active = session.query(Exchange).filter_by(is_active=True).first()
                if active:
                    self.sidebar.set_trading_mode(getattr(active, "mode", "live"))
                else:
                    self.sidebar.set_trading_mode("off")
        except Exception as exc:
            logger.debug("_restore_exchange_mode: %s", exc)

    def _restore_status_bar_exchange(self):
        """
        On startup, read the active exchange name from the DB and update the
        status bar label so it reflects the real exchange (e.g. 'Bybit: Disconnected')
        rather than a hardcoded placeholder.  Called before any EXCHANGE_CONNECTED
        event fires; the label is overwritten again once the exchange connects.
        """
        try:
            from core.database.engine import get_session
            from core.database.models import Exchange
            with get_session() as session:
                active = session.query(Exchange).filter_by(is_active=True).first()
                if active:
                    name = active.name or active.exchange_id or "Exchange"
                    self.status_bar.set_exchange_connected(name, False)
        except Exception as exc:
            logger.debug("_restore_status_bar_exchange: %s", exc)

    def _restore_execution_mode(self):
        """Set initial execution mode indicator from order_router."""
        try:
            from core.execution.order_router import order_router
            self.status_bar.set_execution_mode(order_router.mode)
        except Exception as exc:
            logger.debug("_restore_execution_mode: %s", exc)

    def _navigate_to(self, page_key: str):
        if page_key in self._pages:
            self.stack.setCurrentWidget(self._pages[page_key])
            self.sidebar.set_active(page_key)
            bus.publish(Topics.PAGE_CHANGED, {"page": page_key}, source="main_window")
            logger.debug("Navigated to page: %s", page_key)

    # ── EventBus callbacks (may be called from background threads) ──

    def _on_exchange_connected(self, event):
        data = event.data or {}
        # Emit signal to marshal UI update to the main thread
        self._sig_exchange_connected.emit(
            data.get("name", "Exchange"),
            data.get("connected", False),
        )
        # Update sidebar mode badge from the event payload
        mode = data.get("exchange_mode", "live")
        self._sig_exchange_mode.emit(mode)

    def _on_exchange_error(self, event):
        data = event.data or {} if hasattr(event, "data") else {}
        name   = data.get("name",   "Exchange")
        reason = data.get("reason", "Disconnected")
        self._sig_exchange_error.emit(name, reason)

    def _on_feed_status(self, event):
        data = event.data or {}
        self._sig_feed_status.emit(data.get("active", False))

    def _on_mode_changed(self, event):
        data = event.data or {}
        mode = data.get("new_mode", "paper")
        self._sig_mode_changed.emit(mode)

    # ── Slot handlers (always execute on main thread) ───────────────

    @Slot(str, bool)
    def _update_exchange_status(self, name: str, connected: bool):
        self.status_bar.set_exchange_connected(name, connected)

    @Slot(str, str)
    def _update_exchange_error(self, name: str, reason: str):
        self.status_bar.set_exchange_error(name, reason)

    @Slot(bool)
    def _update_feed_status(self, active: bool):
        self.status_bar.set_feed_active(active)

    def _start_ai_health_check(self):
        """Poll the configured AI provider every 30 s and update the status bar."""
        self._check_ai_status()           # immediate first check
        self._ai_health_timer = QTimer(self)
        self._ai_health_timer.setInterval(30_000)
        self._ai_health_timer.timeout.connect(self._check_ai_status)
        self._ai_health_timer.start()

    def _check_ai_status(self):
        """Run a quick reachability check against the active AI provider (non-blocking)."""
        import threading
        def _probe():
            online = False
            try:
                from config.settings import settings as _s
                provider = _s.get("ai.active_provider", "").strip()

                if "local" in provider.lower() or "ollama" in provider.lower():
                    # Ollama: probe the HTTP endpoint — a configured model name
                    # doesn't mean the server is actually running.
                    import urllib.request
                    url = _s.get("ai.ollama_url", "http://localhost:11434/v1").strip()
                    base = url.replace("/v1", "").rstrip("/")
                    req = urllib.request.urlopen(base, timeout=2)
                    online = req.status < 500

                else:
                    # Cloud providers — keys are stored encrypted in the vault.
                    # config.yaml stores "__vault__" as a placeholder when a key
                    # is saved.  We treat "__vault__" as proof the key exists
                    # without needing to decrypt it in a background thread.
                    key_map = {
                        "anthropic": "ai.anthropic_api_key",
                        "claude":    "ai.anthropic_api_key",
                        "openai":    "ai.openai_api_key",
                        "gemini":    "ai.gemini_api_key",
                    }
                    # Determine which key(s) to check
                    keys_to_check = []
                    p_lower = provider.lower()
                    for keyword, setting_key in key_map.items():
                        if keyword in p_lower and setting_key not in keys_to_check:
                            keys_to_check.append(setting_key)
                    # Auto mode: check all three cloud keys
                    if not keys_to_check:
                        keys_to_check = list(dict.fromkeys(key_map.values()))

                    for setting_key in keys_to_check:
                        val = _s.get(setting_key, "").strip()
                        if val and val not in ("", "sk-...", "..."):
                            online = True
                            break

            except Exception:
                online = False
            self._sig_ai_status.emit(online)
        threading.Thread(target=_probe, daemon=True).start()

    # ── Public UI-test hooks ─────────────────────────────────

    def go_to_page(self, page_key: str) -> bool:
        """
        Public navigation hook for programmatic UI testing.
        Navigate to page_key and return True if the page exists.
        """
        if page_key not in self._pages:
            logger.warning("go_to_page: unknown key '%s'", page_key)
            return False
        self._navigate_to(page_key)
        return True

    def capture_ui(self, name: str, out_dir: str = "") -> str:
        """
        Capture the entire main window using Qt-native QWidget.grab().
        Works in both normal and offscreen (QT_QPA_PLATFORM=offscreen) mode.

        Parameters
        ----------
        name : str
            Base filename (without extension) — spaces/slashes replaced with _.
        out_dir : str
            Directory to save into.  Defaults to artifacts/ui/<timestamp>/.

        Returns the absolute path to the saved PNG.
        """
        from pathlib import Path
        from datetime import datetime
        from PySide6.QtWidgets import QApplication
        if QApplication.instance():
            QApplication.instance().processEvents()

        safe_name = name.replace(" ", "_").replace("/", "_")
        if out_dir:
            save_dir = Path(out_dir)
        else:
            root = Path(__file__).parent.parent
            save_dir = root / "artifacts" / "ui" / datet