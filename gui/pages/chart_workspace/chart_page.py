# ============================================================
# NEXUS TRADER — Chart Workspace Page (Phase 2)
# Full candlestick chart with indicators, scanner, signals
# ============================================================

import logging
from typing import Optional

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QComboBox, QPushButton, QProgressBar, QScrollArea,
    QCheckBox, QGridLayout, QSplitter, QSizePolicy,
    QButtonGroup, QGroupBox, QStackedWidget, QSpinBox
)
from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor

from gui.main_window import PageHeader
from gui.widgets.chart_widget import ChartWidget

logger = logging.getLogger(__name__)


# ── Signal Badge ──────────────────────────────────────────
class SignalBadge(QLabel):
    """Colored badge showing the current market signal."""

    _COLORS = {
        "bullish": ("#00CC77", "#002211"),
        "bearish": ("#FF3355", "#220011"),
        "neutral": ("#8899AA", "#1A2332"),
    }

    def __init__(self, parent=None):
        super().__init__("● NEUTRAL", parent)
        self.setAlignment(Qt.AlignCenter)
        self.setFixedSize(120, 28)
        self.setSignal("neutral", 50)

    def setSignal(self, signal: str, strength: int = 50):
        fg, bg = self._COLORS.get(signal, self._COLORS["neutral"])
        # strength is the bullish vote proportion (0–100).
        # Show the percentage in the direction of the stated signal so
        # "BEARISH 75%" means 75% of indicators are bearish (not 25%).
        if signal == "bearish":
            display_pct = 100 - strength   # bearish confirmation %
        elif signal == "bullish":
            display_pct = strength         # bullish confirmation %
        else:
            display_pct = strength         # neutral — show bullish %
        label = f"● {signal.upper()} {display_pct}%"
        self.setText(label)
        self.setStyleSheet(
            f"background:{bg}; color:{fg}; border:1px solid {fg}; "
            f"border-radius:4px; font-size:13px; font-weight:bold; padding:2px 6px;"
        )


# ── Indicator Toggle Panel ─────────────────────────────────
class IndicatorPanel(QFrame):
    """Collapsible panel with checkboxes for all indicators."""

    indicators_changed = Signal(list)

    # (display_name, column_key)
    INDICATOR_GROUPS = {
        "Trend": [
            ("EMA 9",   "ema_9"),
            ("EMA 20",  "ema_20"),
            ("EMA 50",  "ema_50"),
            ("EMA 200", "ema_200"),
            ("SMA 20",  "sma_20"),
            ("SMA 50",  "sma_50"),
            ("VWAP",    "vwap"),
            ("Supertrend", "supertrend"),
        ],
        "Volatility": [
            ("BB Upper", "bb_upper"),
            ("BB Mid",   "bb_mid"),
            ("BB Lower", "bb_lower"),
            ("KC Upper", "kc_upper"),
            ("KC Lower", "kc_lower"),
        ],
        "Ichimoku": [
            ("Conversion", "ichi_conversion"),
            ("Base",       "ichi_base"),
            ("Cloud A",    "ichi_a"),
            ("Cloud B",    "ichi_b"),
        ],
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self._checkboxes: dict[str, QCheckBox] = {}
        self._defaults = {"ema_20", "ema_50", "bb_upper", "bb_lower", "bb_mid"}
        self._build()

    def _build(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(6)

        title_row = QHBoxLayout()
        title = QLabel("📊 Indicators")
        title.setStyleSheet("font-weight: bold; font-size: 13px; color: #E8EBF0;")
        title_row.addWidget(title, 1)   # stretch=1 so it takes leftover space

        clear_btn = QPushButton("Clear")
        clear_btn.setToolTip("Deselect all indicators")
        clear_btn.setStyleSheet(
            "QPushButton { background: #1A2332; color: #8899AA; border: 1px solid #2A3A52; "
            "border-radius: 3px; font-size: 13px; padding: 2px 8px; }"
            "QPushButton:hover { color: #E8EBF0; border-color: #4A6A8A; }"
        )
        clear_btn.setFixedHeight(22)
        clear_btn.setMinimumWidth(50)
        clear_btn.clicked.connect(self._clear_all)
        title_row.addWidget(clear_btn)
        main_layout.addLayout(title_row)

        # 2-column grid — wide enough for full label names
        grid = QGridLayout()
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(3)

        cb_style = "QCheckBox { font-size: 13px; color: #C0CCD8; spacing: 5px; } " \
                   "QCheckBox::indicator { width: 13px; height: 13px; } " \
                   "QCheckBox:checked { color: #E8EBF0; }"

        grid_row = 0
        for group_name, indicators in self.INDICATOR_GROUPS.items():
            # Group header spanning both columns
            grp_label = QLabel(group_name.upper())
            grp_label.setStyleSheet(
                "color: #1E90FF; font-size: 13px; font-weight: bold; "
                "padding-top: 4px; "
            )
            grid.addWidget(grp_label, grid_row, 0, 1, 2)
            grid_row += 1

            col = 0
            for name, key in indicators:
                cb = QCheckBox(name)
                cb.setChecked(key in self._defaults)
                cb.setStyleSheet(cb_style)
                cb.stateChanged.connect(self._emit_change)
                self._checkboxes[key] = cb
                grid.addWidget(cb, grid_row, col)
                col += 1
                if col == 2:
                    col = 0
                    grid_row += 1

            # ensure next group starts on new row
            if col != 0:
                grid_row += 1

        main_layout.addLayout(grid)

    def _clear_all(self):
        for cb in self._checkboxes.values():
            cb.setChecked(False)

    def _emit_change(self):
        active = [key for key, cb in self._checkboxes.items() if cb.isChecked()]
        self.indicators_changed.emit(active)

    def get_active(self) -> list[str]:
        return [key for key, cb in self._checkboxes.items() if cb.isChecked()]


# ── Chart Workspace Page ───────────────────────────────────
class ChartWorkspacePage(QWidget):
    """
    Full chart workspace:
    - Symbol + timeframe selector
    - Load historical data (with progress bar)
    - Live refresh toggle
    - 30+ indicator overlays
    - Signal badge
    - OHLCV crosshair
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_df = None
        self._current_symbol = ""
        self._current_timeframe = "1h"
        self._loader_worker = None
        self._live_timer = QTimer()
        self._live_timer.setInterval(10000)   # 10 s refresh
        self._live_timer.timeout.connect(self._refresh_chart)
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(PageHeader(
            "Chart Workspace",
            "Candlestick charts with 30+ indicators and real-time signal analysis"
        ))

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(16, 12, 16, 12)
        content_layout.setSpacing(8)

        # ── Toolbar ───────────────────────────────────────
        toolbar = self._build_toolbar()
        content_layout.addWidget(toolbar)

        # ── Progress bar ──────────────────────────────────
        self._progress_bar = QProgressBar()
        self._progress_bar.setTextVisible(True)
        self._progress_bar.setFixedHeight(18)
        self._progress_bar.setVisible(False)
        self._progress_bar.setStyleSheet(
            "QProgressBar { background: #0F1623; border: 1px solid #1A2332; border-radius: 3px; }"
            "QProgressBar::chunk { background: #1E90FF; border-radius: 3px; }"
        )
        content_layout.addWidget(self._progress_bar)

        # ── Main splitter: chart + indicator panel ────────
        splitter = QSplitter(Qt.Horizontal)

        # Chart area — stacked: placeholder on top, chart below
        self._chart_stack = QStackedWidget()

        # Page 0: empty state placeholder
        placeholder = QFrame()
        placeholder.setObjectName("card")
        ph_layout = QVBoxLayout(placeholder)
        ph_layout.setAlignment(Qt.AlignCenter)
        ph_layout.setSpacing(14)

        icon_lbl = QLabel("📈")
        icon_lbl.setAlignment(Qt.AlignCenter)
        icon_lbl.setStyleSheet("font-size: 52px;")
        ph_layout.addWidget(icon_lbl)

        hint_title = QLabel("No chart data loaded")
        hint_title.setAlignment(Qt.AlignCenter)
        hint_title.setStyleSheet("color: #E8EBF0; font-size: 16px; font-weight: 700;")
        ph_layout.addWidget(hint_title)

        hint_sub = QLabel(
            "1.  Type or select a symbol above  (e.g. BTC/USDT)\n"
            "2.  Choose a timeframe  (1h is a good start)\n"
            "3.  Click the orange  ⬇ Load Data  button"
        )
        hint_sub.setAlignment(Qt.AlignCenter)
        hint_sub.setStyleSheet("color: #5A7A9A; font-size: 13px; line-height: 1.6;")
        ph_layout.addWidget(hint_sub)

        self._chart_stack.addWidget(placeholder)   # index 0

        # Page 1: actual chart
        self._chart = ChartWidget()
        self._chart_stack.addWidget(self._chart)   # index 1

        splitter.addWidget(self._chart_stack)

        # Right panel: indicators
        right_panel = QWidget()
        right_panel.setFixedWidth(270)
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)

        self._indicator_panel = IndicatorPanel()
        self._indicator_panel.indicators_changed.connect(self._on_indicators_changed)
        right_layout.addWidget(self._indicator_panel)

        # Signal summary card
        self._signal_card = self._build_signal_card()
        right_layout.addWidget(self._signal_card)

        # Trade overlay controls card
        self._overlay_card = self._build_overlay_card()
        right_layout.addWidget(self._overlay_card)

        right_layout.addStretch()
        splitter.addWidget(right_panel)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        content_layout.addWidget(splitter, 1)

        layout.addWidget(content, 1)

    def _build_toolbar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("card")
        # Two-row toolbar: row 1 = symbol/TF/buttons, row 2 = status
        v = QVBoxLayout(bar)
        v.setContentsMargins(14, 10, 14, 10)
        v.setSpacing(6)

        # ── Row 1 ────────────────────────────────────────
        row1 = QHBoxLayout()
        row1.setSpacing(10)

        # Symbol label + combo
        sym_label = QLabel("Symbol:")
        sym_label.setStyleSheet("color: #8899AA; font-size: 13px; font-weight: 600;")
        sym_label.setFixedWidth(52)
        row1.addWidget(sym_label)

        self._symbol_combo = QComboBox()
        self._symbol_combo.setFixedSize(160, 34)
        self._symbol_combo.setEditable(False)
        self._symbol_combo.setStyleSheet(
            "QComboBox { background: #131B2A; color: #E8EBF0; border: 1px solid #2A3A52; "
            "border-radius: 5px; padding: 4px 10px; font-size: 13px; font-weight: 600; }"
            "QComboBox:focus { border-color: #1E90FF; }"
            "QComboBox QAbstractItemView { background: #131B2A; color: #E8EBF0; "
            "selection-background-color: #1A2D4A; }"
        )
        self._populate_symbol_combo()
        row1.addWidget(self._symbol_combo)

        # Divider
        div = QLabel("|")
        div.setStyleSheet("color: #2A3A52; font-size: 18px;")
        row1.addWidget(div)

        # Timeframe label
        tf_label = QLabel("TF:")
        tf_label.setStyleSheet("color: #8899AA; font-size: 13px; font-weight: 600;")
        row1.addWidget(tf_label)

        # Timeframe buttons
        self._tf_group = QButtonGroup(self)
        tf_style = (
            "QPushButton { background:#131B2A; color:#8899AA; border:1px solid #2A3A52; "
            "border-radius:4px; font-size:13px; font-weight:600; padding:0 4px; }"
            "QPushButton:checked { background:#1E4A8A; color:#FFFFFF; border-color:#1E90FF; }"
            "QPushButton:hover:!checked { background:#1A2332; border-color:#1E90FF; color:#C0D0E0; }"
        )
        for tf in ["1m", "5m", "15m", "1h", "4h", "1d"]:
            btn = QPushButton(tf)
            btn.setCheckable(True)
            btn.setFixedSize(46, 34)
            btn.setChecked(tf == "1h")
            btn.setStyleSheet(tf_style)
            btn.clicked.connect(lambda checked, t=tf: self._on_timeframe_changed(t))
            self._tf_group.addButton(btn)
            row1.addWidget(btn)

        row1.addSpacing(12)

        # Load Data button — prominent orange
        self._load_btn = QPushButton("⬇  Load Data")
        self._load_btn.setMinimumWidth(130)
        self._load_btn.setFixedHeight(34)
        self._load_btn.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        self._load_btn.setStyleSheet(
            "QPushButton { background: #E87820; color: #FFFFFF; border: none; "
            "border-radius: 5px; font-size: 13px; font-weight: 700; padding: 0 16px; }"
            "QPushButton:hover { background: #FF8C30; }"
            "QPushButton:pressed { background: #C06010; }"
            "QPushButton:disabled { background: #3A2A10; color: #886633; }"
        )
        self._load_btn.clicked.connect(self._load_data)
        row1.addWidget(self._load_btn)

        # Live toggle
        self._live_btn = QPushButton("▶  Live")
        self._live_btn.setCheckable(True)
        self._live_btn.setFixedSize(80, 34)
        self._live_btn.setStyleSheet(
            "QPushButton { background:#131B2A; color:#8899AA; border:1px solid #2A3A52; "
            "border-radius:5px; font-size:13px; font-weight:600; }"
            "QPushButton:checked { background:#003322; color:#00CC77; border-color:#00CC77; }"
            "QPushButton:hover:!checked { border-color:#00CC77; }"
        )
        self._live_btn.toggled.connect(self._on_live_toggled)
        row1.addWidget(self._live_btn)

        row1.addStretch()
        v.addLayout(row1)

        # ── Row 2: status strip ──────────────────────────
        self._status_label = QLabel("← Select a symbol and click  ⬇ Load Data  to begin")
        self._status_label.setStyleSheet("color: #5A7A9A; font-size: 13px;")
        v.addWidget(self._status_label)

        return bar

    def _build_signal_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        title = QLabel("Signal Analysis")
        title.setStyleSheet("font-weight: bold; font-size: 13px; color: #E8EBF0;")
        layout.addWidget(title)

        self._signal_badge = SignalBadge()
        layout.addWidget(self._signal_badge)

        self._signal_reasons = QLabel("—")
        self._signal_reasons.setWordWrap(True)
        self._signal_reasons.setStyleSheet("color: #8899AA; font-size: 13px;")
        layout.addWidget(self._signal_reasons)

        # Key metrics
        metrics_layout = QGridLayout()
        metrics_layout.setSpacing(4)

        self._rsi_label   = self._metric_label("RSI", "—")
        self._vol_label   = self._metric_label("Volume", "—")

        metrics_layout.addWidget(QLabel("RSI:"),    0, 0)
        metrics_layout.addWidget(self._rsi_label,   0, 1)
        metrics_layout.addWidget(QLabel("Volume:"), 1, 0)
        metrics_layout.addWidget(self._vol_label,   1, 1)

        for i in range(metrics_layout.count()):
            w = metrics_layout.itemAt(i).widget()
            if isinstance(w, QLabel):
                w.setStyleSheet("color: #8899AA; font-size: 13px;")

        layout.addLayout(metrics_layout)
        return card

    def _metric_label(self, key: str, value: str) -> QLabel:
        lbl = QLabel(value)
        lbl.setStyleSheet("color: #E8EBF0; font-size: 13px; font-weight: bold;")
        return lbl

    # ── Population helpers ─────────────────────────────────
    # Fixed pairs available for chart selection
    _CHART_PAIRS = [
        "BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT", "SOL/USDT",
        "TRX/USDT", "DOGE/USDT", "ADA/USDT", "BCH/USDT", "HYPE/USDT",
        "LINK/USDT", "XLM/USDT", "AVAX/USDT", "HBAR/USDT", "SUI/USDT",
        "NEAR/USDT", "ICP/USDT", "ONDO/USDT", "ALGO/USDT", "RENDER/USDT",
    ]

    def _populate_symbol_combo(self, quote: str = "USDT"):
        """Populate the symbol combo with the four supported trading pairs."""
        current_text = self._symbol_combo.currentText()
        symbols = self._CHART_PAIRS

        self._symbol_combo.blockSignals(True)
        self._symbol_combo.clear()
        self._symbol_combo.addItems(symbols)
        # Restore previous selection if it is one of the supported pairs
        if current_text in symbols:
            self._symbol_combo.setCurrentText(current_text)
        self._symbol_combo.blockSignals(False)

    # ── Event handlers ─────────────────────────────────────
    def _on_timeframe_changed(self, tf: str):
        self._current_timeframe = tf

    def _on_indicators_changed(self, active: list[str]):
        self._chart.set_indicators(active)

    def _on_live_toggled(self, checked: bool):
        if checked:
            self._live_timer.start()
            self._status_label.setText("🟢 Live refresh ON")
        else:
            self._live_timer.stop()
            self._status_label.setText("⏹ Live refresh OFF")

    # ── Data loading ───────────────────────────────────────
    def _load_data(self):
        symbol = self._symbol_combo.currentText().strip()
        if not symbol:
            return

        self._current_symbol    = symbol
        self._current_timeframe = self._get_selected_timeframe()

        # Try to load from DB first
        try:
            from core.database.engine import get_session
            from core.database.models import Asset
            with get_session() as session:
                asset = session.query(Asset).filter_by(symbol=symbol).first()
                asset_id = asset.id if asset else None
        except Exception:
            asset_id = None

        if asset_id:
            self._load_from_db(asset_id)
        else:
            self._download_from_exchange(symbol)

    def _get_selected_timeframe(self) -> str:
        for btn in self._tf_group.buttons():
            if btn.isChecked():
                return btn.text()
        return "1h"

    # Minimum hours a candle must be "behind" before we consider data stale
    _STALE_MULTIPLIER = 2   # 2× the timeframe

    _TF_HOURS = {
        "1m": 1/60, "3m": 3/60, "5m": 5/60, "15m": 0.25, "30m": 0.5,
        "1h": 1, "2h": 2, "4h": 4, "6h": 6, "8h": 8, "12h": 12,
        "1d": 24, "3d": 72, "1w": 168,
    }

    def _load_from_db(self, asset_id: int):
        """
        Load OHLCV from SQLite and render.

        After rendering, checks whether the newest candle is older than
        2× the current timeframe.  If so, automatically triggers a
        gap-fill download from the exchange so the chart catches up to
        the present without any manual action from the user.
        """
        try:
            from core.market_data.historical_loader import load_ohlcv_from_db
            import pandas as pd
            df = load_ohlcv_from_db(asset_id, self._current_timeframe, limit=1000)
            if df is not None and not df.empty:
                self._current_df = df
                self._refresh_chart()

                # ── Freshness check ───────────────────────────────────
                latest_ts = df.index[-1]
                # Make UTC-naive for comparison (DB stores naive UTC)
                now_utc = pd.Timestamp.utcnow().tz_localize(None)
                hours_behind = (now_utc - latest_ts).total_seconds() / 3600

                tf_h = self._TF_HOURS.get(self._current_timeframe, 1)
                stale_threshold = tf_h * self._STALE_MULTIPLIER

                if hours_behind > stale_threshold:
                    # Data is old — show what we have then auto-fill the gap
                    behind_str = (
                        f"{int(hours_behind)}h" if hours_behind < 48
                        else f"{int(hours_behind / 24)}d"
                    )
                    self._status_label.setText(
                        f"✓ {len(df):,} candles (DB, {behind_str} behind) "
                        f"— fetching updates…"
                    )
                    self._fetch_gap(asset_id, latest_ts)
                else:
                    self._status_label.setText(
                        f"✓ {self._current_symbol} {self._current_timeframe}"
                        f" — {len(df):,} candles (DB)"
                    )
                return
        except Exception as e:
            logger.warning("DB load failed: %s", e)

        # Fallback: download full history
        self._download_from_exchange(self._current_symbol)

    def _fetch_gap(self, asset_id: int, since_ts):
        """
        Download only the candles newer than *since_ts* (gap-fill).
        Shows the chart with existing data immediately; new candles are
        appended once the background download completes.
        """
        import pandas as pd
        from core.market_data.exchange_manager import exchange_manager

        if not exchange_manager.is_connected():
            try:
                exchange_manager.load_active_exchange()
            except Exception:
                pass

        if not exchange_manager.is_connected():
            hours_behind = (
                pd.Timestamp.utcnow().tz_localize(None) - since_ts
            ).total_seconds() / 3600
            behind_str = (
                f"{int(hours_behind)}h" if hours_behind < 48
                else f"{int(hours_behind / 24)}d"
            )
            self._status_label.setText(
                f"⚠ Not connected — data is {behind_str} behind. "
                f"Go to Exchange Management to reconnect."
            )
            return

        # Convert last known timestamp to ms (exclusive)
        since_ms = int(since_ts.timestamp() * 1000) + 1

        if self._loader_worker and self._loader_worker.isRunning():
            self._loader_worker.stop()
            self._loader_worker.wait(2000)

        from core.market_data.historical_loader import HistoricalLoaderWorker
        self._loader_worker = HistoricalLoaderWorker(
            symbol=self._current_symbol,
            timeframe=self._current_timeframe,
            asset_id=asset_id,
            since_ms=since_ms,
        )
        self._loader_worker.progress.connect(self._on_download_progress)
        self._loader_worker.finished.connect(self._on_gap_fill_finished)
        self._loader_worker.error.connect(self._on_download_error)

        self._progress_bar.setValue(0)
        self._progress_bar.setVisible(True)
        self._load_btn.setEnabled(False)
        self._loader_worker.start()

    @Slot(str, int)
    def _on_gap_fill_finished(self, symbol: str, new_rows: int):
        """
        Reload from DB after gap-fill to show the combined fresh data.

        After reloading, re-applies the same freshness check used by
        _load_from_db() so that large gaps (> 1 batch) are filled
        automatically without requiring repeated manual "Load Data" clicks.
        """
        self._progress_bar.setVisible(False)
        self._load_btn.setEnabled(True)
        try:
            from core.database.engine import get_session
            from core.database.models import Asset
            from core.market_data.historical_loader import load_ohlcv_from_db
            import pandas as pd
            with get_session() as session:
                asset = session.query(Asset).filter_by(symbol=symbol).first()
                asset_id = asset.id if asset else None
            if asset_id:
                df = load_ohlcv_from_db(asset_id, self._current_timeframe, limit=1000)
                if df is not None and not df.empty:
                    self._current_df = df
                    self._refresh_chart()

                    # ── Freshness re-check: chain another fill if still behind ──
                    latest_ts  = df.index[-1]
                    now_utc    = pd.Timestamp.utcnow().tz_localize(None)
                    hours_behind = (now_utc - latest_ts).total_seconds() / 3600
                    tf_h = self._TF_HOURS.get(self._current_timeframe, 1)
                    stale_threshold = tf_h * self._STALE_MULTIPLIER

                    if hours_behind > stale_threshold:
                        # Still behind — trigger another gap fill automatically
                        behind_str = (
                            f"{int(hours_behind)}h" if hours_behind < 48
                            else f"{int(hours_behind / 24)}d"
                        )
                        self._status_label.setText(
                            f"✓ {len(df):,} candles (+{new_rows:,} new,"
                            f" still {behind_str} behind) — fetching more…"
                        )
                        self._fetch_gap(asset_id, latest_ts)
                    else:
                        self._status_label.setText(
                            f"✓ {symbol} up to date — {len(df):,} candles"
                            + (f" (+{new_rows:,} new)" if new_rows else "")
                        )
                    return
        except Exception as e:
            logger.warning("Post-gap-fill DB load failed: %s", e)

        if new_rows:
            self._status_label.setText(
                f"✓ Gap fill complete — +{new_rows:,} new candles added"
            )
        else:
            self._status_label.setText(
                f"✓ {symbol} already up to date"
            )

    def _download_from_exchange(self, symbol: str):
        """Download OHLCV from exchange via background worker."""
        try:
            from core.market_data.exchange_manager import exchange_manager
            # Auto-connect if we have a saved active exchange
            if not exchange_manager.is_connected():
                self._status_label.setText("⟳ Connecting to exchange…")
                try:
                    exchange_manager.load_active_exchange()
                except Exception as e:
                    logger.warning("Auto-connect failed: %s", e)
            if not exchange_manager.is_connected():
                self._status_label.setText(
                    "⚠ Not connected — go to Exchange Management → Test Connection first"
                )
                return

            from core.database.engine import get_session
            from core.database.models import Asset, Exchange as ExchangeModel
            with get_session() as session:
                exch = session.query(ExchangeModel).filter_by(is_active=True).first()
                exch_id = exch.id if exch else None
                if exch_id:
                    asset = session.query(Asset).filter_by(symbol=symbol).first()
                    if not asset:
                        market = exchange_manager.get_markets().get(symbol, {})
                        from core.database.models import Asset
                        from datetime import datetime
                        new_asset = Asset(
                            exchange_id=exch_id,
                            symbol=symbol,
                            base_currency=market.get("base", symbol.split("/")[0]),
                            quote_currency=market.get("quote", "USDT"),
                            last_updated=datetime.utcnow(),
                        )
                        session.add(new_asset)
                        session.flush()
                        asset_id = new_asset.id
                    else:
                        asset_id = asset.id
                else:
                    self._status_label.setText("⚠ No active exchange in DB")
                    return

        except Exception as e:
            logger.warning("Asset lookup error: %s", e)
            self._status_label.setText(f"⚠ Error: {e}")
            return

        # Stop existing worker
        if self._loader_worker and self._loader_worker.isRunning():
            self._loader_worker.stop()
            self._loader_worker.wait(2000)

        from core.market_data.historical_loader import HistoricalLoaderWorker
        self._loader_worker = HistoricalLoaderWorker(
            symbol=symbol,
            timeframe=self._current_timeframe,
            days_back=90,
            asset_id=asset_id,
        )
        self._loader_worker.progress.connect(self._on_download_progress)
        self._loader_worker.finished.connect(self._on_download_finished)
        self._loader_worker.error.connect(self._on_download_error)

        self._progress_bar.setValue(0)
        self._progress_bar.setVisible(True)
        self._load_btn.setEnabled(False)
        self._status_label.setText(f"Downloading {symbol}...")
        self._loader_worker.start()

    @Slot(int, int, str)
    def _on_download_progress(self, current: int, total: int, message: str):
        if total > 0:
            pct = min(int(current / total * 100), 100)
            self._progress_bar.setValue(pct)
            self._progress_bar.setFormat(f"{message} ({pct}%)")

    @Slot(str, int)
    def _on_download_finished(self, symbol: str, rows: int):
        self._progress_bar.setValue(100)
        self._progress_bar.setVisible(False)
        self._load_btn.setEnabled(True)
        self._status_label.setText(f"✓ Downloaded {rows:,} candles for {symbol}")
        # Now load from DB
        try:
            from core.database.engine import get_session
            from core.database.models import Asset
            with get_session() as session:
                asset = session.query(Asset).filter_by(symbol=symbol).first()
                asset_id = asset.id if asset else None
            if asset_id:
                self._load_from_db(asset_id)
        except Exception as e:
            logger.warning("Post-download DB load failed: %s", e)

    @Slot(str, str)
    def _on_download_error(self, symbol: str, error: str):
        self._progress_bar.setVisible(False)
        self._load_btn.setEnabled(True)
        self._status_label.setText(f"⚠ Error: {error}")

    def _build_overlay_card(self) -> QFrame:
        """Right-panel card with trade overlay display controls."""
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        title = QLabel("📍 Trade Overlay")
        title.setStyleSheet("font-weight: bold; font-size: 13px; color: #E8EBF0;")
        layout.addWidget(title)

        cb_style = (
            "QCheckBox { font-size: 13px; color: #C0CCD8; spacing: 5px; } "
            "QCheckBox::indicator { width: 13px; height: 13px; } "
            "QCheckBox:checked { color: #E8EBF0; }"
        )

        # Master visibility toggle
        # NOTE: stateChanged must be connected BEFORE setChecked(True).
        # If connected after, the initial setChecked emits stateChanged but
        # no slot is attached yet → _on_overlay_show_changed never fires at
        # startup → overlay stays setVisible(False) even though the checkbox
        # appears checked (primary root cause of "overlay always empty" bug).
        self._overlay_show_cb = QCheckBox("Show Trade Overlay")
        self._overlay_show_cb.setStyleSheet(cb_style)
        self._overlay_show_cb.stateChanged.connect(self._on_overlay_show_changed)
        self._overlay_show_cb.setChecked(True)   # fires slot immediately ↑
        layout.addWidget(self._overlay_show_cb)

        # Detail toggles (indented slightly)
        detail_frame = QFrame()
        detail_layout = QVBoxLayout(detail_frame)
        detail_layout.setContentsMargins(12, 2, 0, 2)
        detail_layout.setSpacing(3)

        self._overlay_bars_cb = QCheckBox("Duration Bars")
        self._overlay_bars_cb.setChecked(True)
        self._overlay_bars_cb.setStyleSheet(cb_style)
        self._overlay_bars_cb.stateChanged.connect(self._on_overlay_option_changed)
        detail_layout.addWidget(self._overlay_bars_cb)

        self._overlay_conn_cb = QCheckBox("Connection Lines")
        self._overlay_conn_cb.setChecked(True)
        self._overlay_conn_cb.setStyleSheet(cb_style)
        self._overlay_conn_cb.stateChanged.connect(self._on_overlay_option_changed)
        detail_layout.addWidget(self._overlay_conn_cb)

        self._overlay_exit_cb = QCheckBox("Exit Quality Dots")
        self._overlay_exit_cb.setChecked(True)
        self._overlay_exit_cb.setStyleSheet(cb_style)
        self._overlay_exit_cb.stateChanged.connect(self._on_overlay_option_changed)
        detail_layout.addWidget(self._overlay_exit_cb)

        layout.addWidget(detail_frame)

        # Filter mode row
        filter_row = QHBoxLayout()
        filter_lbl = QLabel("Show:")
        filter_lbl.setStyleSheet("color: #8899AA; font-size: 13px;")
        filter_row.addWidget(filter_lbl)

        self._overlay_filter_combo = QComboBox()
        self._overlay_filter_combo.addItems(["All Trades", "Open Only", "Closed Only"])
        self._overlay_filter_combo.setStyleSheet(
            "QComboBox { background: #131B2A; color: #E8EBF0; border: 1px solid #2A3A52; "
            "border-radius: 4px; padding: 2px 8px; font-size: 13px; }"
            "QComboBox QAbstractItemView { background: #131B2A; color: #E8EBF0; "
            "selection-background-color: #1A2D4A; }"
        )
        self._overlay_filter_combo.setFixedHeight(28)
        self._overlay_filter_combo.currentIndexChanged.connect(self._on_overlay_option_changed)
        filter_row.addWidget(self._overlay_filter_combo, 1)
        layout.addLayout(filter_row)

        # Last N trades row
        lastn_row = QHBoxLayout()
        lastn_lbl = QLabel("Last N:")
        lastn_lbl.setStyleSheet("color: #8899AA; font-size: 13px;")
        lastn_row.addWidget(lastn_lbl)

        self._overlay_lastn_spin = QSpinBox()
        self._overlay_lastn_spin.setRange(0, 500)
        self._overlay_lastn_spin.setValue(0)
        self._overlay_lastn_spin.setSpecialValueText("All")
        self._overlay_lastn_spin.setFixedHeight(28)
        self._overlay_lastn_spin.setStyleSheet(
            "QSpinBox { background: #131B2A; color: #E8EBF0; border: 1px solid #2A3A52; "
            "border-radius: 4px; padding: 2px 6px; font-size: 13px; }"
        )
        self._overlay_lastn_spin.valueChanged.connect(self._on_overlay_option_changed)
        lastn_row.addWidget(self._overlay_lastn_spin, 1)
        layout.addLayout(lastn_row)

        # Reload button
        reload_btn = QPushButton("↺  Reload Trades")
        reload_btn.setFixedHeight(28)
        reload_btn.setStyleSheet(
            "QPushButton { background: #1A2332; color: #8899AA; border: 1px solid #2A3A52; "
            "border-radius: 4px; font-size: 13px; }"
            "QPushButton:hover { color: #E8EBF0; border-color: #1E90FF; }"
        )
        reload_btn.clicked.connect(self._load_trade_data)
        layout.addWidget(reload_btn)

        return card

    # ── Overlay event handlers ──────────────────────────────
    def _on_overlay_show_changed(self, state: int):
        visible = bool(state)
        self._chart.set_overlay_visible(visible)
        # When the overlay is enabled (or re-enabled after being hidden),
        # reload trade data so open positions and any new closed trades are
        # picked up immediately — even if the chart was loaded while the
        # overlay was off.
        if visible:
            self._load_trade_data()

    def _on_overlay_option_changed(self, *_):
        filter_map = {0: "all", 1: "open", 2: "closed"}
        self._chart.set_overlay_options(
            show_duration_bars = self._overlay_bars_cb.isChecked(),
            show_connections   = self._overlay_conn_cb.isChecked(),
            show_exit_quality  = self._overlay_exit_cb.isChecked(),
            filter_mode        = filter_map.get(self._overlay_filter_combo.currentIndex(), "all"),
            last_n             = self._overlay_lastn_spin.value(),
        )
        # Reload data to apply new filter immediately
        self._load_trade_data()

    # ── Trade data loading for overlay ─────────────────────
    def _load_trade_data(self):
        """
        Load closed trades from DB + open positions and push to chart overlay.
        Only loads trades for the currently displayed symbol.
        Non-fatal — overlay stays empty if DB/executor unavailable.
        """
        try:
            closed_trades: list = []
            # Query DB for closed paper trades for this symbol
            from core.database.engine import get_session
            from core.database.models import PaperTrade
            with get_session() as session:
                rows = (
                    session.query(PaperTrade)
                    .filter(PaperTrade.symbol == self._current_symbol)
                    .order_by(PaperTrade.id)
                    .all()
                )
                for row in rows:
                    d = row.to_dict()
                    d["id"] = row.id   # trade_overlay needs a stable unique id
                    closed_trades.append(d)
        except Exception as exc:
            logger.debug("Trade overlay: DB load error: %s", exc)
            closed_trades = []

        try:
            from core.execution.order_router import order_router
            _pe = order_router.active_executor
            open_positions = _pe.get_open_positions()
        except Exception as exc:
            logger.debug("Trade overlay: position load error: %s", exc)
            open_positions = []

        self._chart.set_trade_data(closed_trades, open_positions)

    # ── Chart rendering ────────────────────────────────────
    def _refresh_chart(self):
        """Recalculate indicators and re-render chart."""
        if self._current_df is None or self._current_df.empty:
            return
        try:
            from core.features.indicator_library import calculate_all, get_signals
            df_with_indicators = calculate_all(self._current_df)
            active_indicators  = self._indicator_panel.get_active()

            self._chart.load_dataframe(
                df_with_indicators,
                symbol=self._current_symbol,
                timeframe=self._current_timeframe,
            )
            self._chart.set_indicators(active_indicators)

            # Switch from placeholder to chart
            self._chart_stack.setCurrentIndex(1)

            # Update signal badge
            signals = get_signals(df_with_indicators)
            self._signal_badge.setSignal(signals["signal"], signals["strength"])

            reasons = signals.get("reasons", [])
            self._signal_reasons.setText("\n".join(f"• {r}" for r in reasons) if reasons else "No signals")

            rsi = signals.get("rsi")
            self._rsi_label.setText(f"{rsi:.1f}" if rsi else "—")

            # Volume (last candle)
            if "volume" in df_with_indicators.columns:
                vol = df_with_indicators["volume"].iloc[-1]
                self._vol_label.setText(f"{vol:,.0f}")

            # Trade overlay — push latest trade data after chart is rendered
            self._load_trade_data()

        except Exception as e:
            logger.error("Chart refresh error: %s", e, exc_info=True)

    # ── Called when page becomes visible ──────────────────
    def showEvent(self, event):
        super().showEvent(event)
        self._populate_symbol_combo()
