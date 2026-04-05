# ============================================================
# NEXUS TRADER — IDSS Strategy Evaluation Interface
#
# Professional backtesting interface for IDSS signal pipeline.
# - Bar-by-bar OHLCV replay through RegimeClassifier → SignalGenerator → ConfluenceScorer → RiskGate
# - Configurable timeframe, data range, trading capital, and disabled models
# - Real-time progress feedback (bar-by-bar, data fetch)
# - Equity curve, KPI summary, detailed trade log
# - Save/compare results, export CSV
# ============================================================

import logging
import json
from datetime import datetime, timedelta
from typing import Optional, Dict, List

import numpy as np
import pandas as pd

# pyqtgraph — catch ANY exception during import (not just ImportError)
# to prevent a startup crash from killing the entire application
try:
    import pyqtgraph as pg
    _PG_AVAILABLE = True
except Exception:
    pg = None
    _PG_AVAILABLE = False

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QFrame,
    QPushButton, QComboBox, QDoubleSpinBox, QSpinBox,
    QDateEdit, QTableWidget, QTableWidgetItem, QHeaderView,
    QTabWidget, QProgressBar, QScrollArea, QSplitter,
    QCheckBox, QLineEdit, QMessageBox, QGroupBox,
    QAbstractItemView, QFormLayout, QSizePolicy
)
from PySide6.QtCore import Qt, QThread, Signal, Slot, QDate, QTimer
from PySide6.QtGui import QFont, QColor, QBrush

from gui.main_window import PageHeader

logger = logging.getLogger(__name__)

# ── Style constants ───────────────────────────────────────────
_C_BG      = "#0A0E1A"
_C_CARD    = "#0F1623"
_C_BORDER  = "#1A2332"
_C_TEXT    = "#E8EBF0"
_C_MUTED   = "#8899AA"
_C_BULL    = "#00CC77"
_C_BEAR    = "#FF3355"
_C_GOLD    = "#FFB300"
_C_BLUE    = "#4488CC"
_C_SHADOW  = "#0D1118"

_CARD_STYLE = (
    f"background-color: {_C_CARD}; border: 1px solid {_C_BORDER}; "
    f"border-radius: 6px; padding: 16px;"
)

_COMBO = (
    f"QComboBox {{ background: {_C_CARD}; color: {_C_TEXT}; border: 1px solid {_C_BORDER}; "
    f"border-radius: 4px; padding: 4px 8px; font-size: 12px; min-height: 28px; }}"
    f"QComboBox:focus {{ border-color: {_C_BLUE}; }}"
    f"QComboBox QAbstractItemView {{ background: {_C_CARD}; color: {_C_TEXT}; "
    f"selection-background-color: {_C_BORDER}; }}"
)

_SPIN = (
    f"QDoubleSpinBox, QSpinBox {{ background: {_C_CARD}; color: {_C_TEXT}; "
    f"border: 1px solid {_C_BORDER}; border-radius: 4px; padding: 4px 8px; "
    f"font-size: 12px; min-height: 28px; }}"
    f"QDoubleSpinBox:focus, QSpinBox:focus {{ border-color: {_C_BLUE}; }}"
)

_DATE = (
    f"QDateEdit {{ background: {_C_CARD}; color: {_C_TEXT}; border: 1px solid {_C_BORDER}; "
    f"border-radius: 4px; padding: 4px 8px; font-size: 12px; min-height: 28px; }}"
    f"QDateEdit:focus {{ border-color: {_C_BLUE}; }}"
)

_BTN_PRIMARY = (
    f"QPushButton {{ background: {_C_BLUE}; color: {_C_TEXT}; border: 0px; "
    f"border-radius: 4px; font-weight: bold; padding: 8px 16px; font-size: 12px; }}"
    f"QPushButton:hover {{ background: #5599DD; }}"
    f"QPushButton:pressed {{ background: #3366AA; }}"
)

_BTN_SECONDARY = (
    f"QPushButton {{ background: {_C_BORDER}; color: {_C_MUTED}; border: 1px solid {_C_BORDER}; "
    f"border-radius: 4px; padding: 6px 12px; font-size: 12px; }}"
    f"QPushButton:hover {{ color: {_C_TEXT}; border-color: #3A4A62; }}"
)

_CHECKBOX = (
    f"QCheckBox {{ color: {_C_TEXT}; spacing: 6px; font-size: 12px; }}"
    f"QCheckBox::indicator {{ width: 16px; height: 16px; }}"
    f"QCheckBox::indicator:unchecked {{ background: {_C_CARD}; border: 1px solid {_C_BORDER}; border-radius: 2px; }}"
    f"QCheckBox::indicator:checked {{ background: {_C_BLUE}; border: 1px solid {_C_BLUE}; }}"
)


# ── Custom PyQtGraph Axis ─────────────────────────────────────
_DateAxisItem_base = pg.AxisItem if _PG_AVAILABLE else object


class _DateAxisItem(_DateAxisItem_base):
    """X-axis for equity curve — displays dates in human-readable format."""

    _LEVELS = [
        (5 * 365 * 86400,  2 * 365 * 86400, "%Y"),
        (2 * 365 * 86400,      365 * 86400, "%Y"),
        (    365 * 86400,       90 * 86400, "%b '%y"),
        (     90 * 86400,       30 * 86400, "%b '%y"),
        (     30 * 86400,        7 * 86400, "%b %d"),
        (     14 * 86400,        2 * 86400, "%b %d"),
        (      3 * 86400,            86400, "%b %d"),
        (          86400,         6 * 3600, "%b %d %H:%M"),
        (       6 * 3600,             3600, "%H:%M"),
    ]

    def tickSpacing(self, minVal, maxVal, size):
        """Return (major_spacing, minor_spacing) based on visible range."""
        span = maxVal - minVal
        for threshold, spacing, _ in self._LEVELS:
            if span >= threshold:
                return [(spacing, 0)]
        return [(3600, 0)]

    def tickStrings(self, values, scale, spacing):
        """Convert Unix timestamps to readable date strings."""
        result = []
        for value in values:
            if value == 0 or value < 86400:  # Suppress epoch/near-zero values
                result.append("")
            else:
                try:
                    dt = datetime.utcfromtimestamp(value)
                    for threshold, _, fmt in self._LEVELS:
                        if (max(values) - min(values)) >= threshold:
                            result.append(dt.strftime(fmt))
                            break
                    else:
                        result.append(dt.strftime("%H:%M"))
                except Exception:
                    result.append("")
        return result


# ── Backtest Worker ───────────────────────────────────────────
class IDSSBacktestWorker(QThread):
    """
    Background worker for IDSS strategy backtesting.
    Fetches data, runs pipeline, computes KPIs, emits progress/results.
    """

    progress = Signal(str)  # Status message
    finished = Signal(dict)  # Result dict
    error    = Signal(str)  # Error message

    def __init__(
        self,
        symbol: str,
        timeframe: str,
        start_date: str,
        end_date: str,
        initial_capital: float,
        fee_pct: float,
        slippage_pct: float,
        disabled_models: List[str],
        confluence_threshold: float,
        parent=None,
    ):
        super().__init__(parent)
        self.symbol = symbol
        self.timeframe = timeframe
        self.start_date = start_date
        self.end_date = end_date
        self.initial_capital = initial_capital
        self.fee_pct = fee_pct
        self.slippage_pct = slippage_pct
        self.disabled_models = disabled_models
        self.confluence_threshold = confluence_threshold

    def run(self):
        """Execute the backtest pipeline."""
        try:
            self.progress.emit("Fetching historical data...")

            # Fetch OHLCV data
            from core.backtesting.data_loader import HistoricalDataLoader
            loader = HistoricalDataLoader()
            df = loader.fetch_ohlcv(
                self.symbol,
                self.timeframe,
                start_date=self.start_date,
                end_date=self.end_date,
                min_bars=100,
            )
            self.progress.emit(f"Fetched {len(df)} bars | Computing indicators...")

            # Calculate all indicators
            from core.features.indicator_library import calculate_all
            df = calculate_all(df)
            self.progress.emit(f"Running IDSS pipeline ({len(df)} bars)...")

            # Run IDSS backtest
            from core.backtesting.idss_backtester import IDSSBacktester

            # Temporarily set disabled_models in config if provided
            if self.disabled_models:
                from config.settings import settings
                old_disabled = settings.get("disabled_models", [])
                settings.set("disabled_models", self.disabled_models)

            try:
                backtester = IDSSBacktester(
                    min_confluence_score=self.confluence_threshold
                )
                result = backtester.run(
                    df=df,
                    symbol=self.symbol,
                    timeframe=self.timeframe,
                    initial_capital=self.initial_capital,
                    fee_pct=self.fee_pct,
                    slippage_pct=self.slippage_pct,
                )

                self.progress.emit("Computing KPIs...")

                # Compute KPIs
                from core.backtesting.kpi_engine import compute_kpis
                kpis = compute_kpis(
                    trades=result.get("trades", []),
                    equity_curve=result.get("equity_curve", []),
                    initial_capital=self.initial_capital,
                    total_bars=len(df),
                    timeframe_seconds={"1h": 3600, "4h": 14400, "1d": 86400}.get(
                        self.timeframe, 3600
                    ),
                )

                # Package result
                result_dict = {
                    "trades": result.get("trades", []),
                    "equity_curve": result.get("equity_curve", []),
                    "kpis": kpis,
                    "symbol": self.symbol,
                    "timeframe": self.timeframe,
                    "start_date": self.start_date,
                    "end_date": self.end_date,
                    "initial_capital": self.initial_capital,
                    "fee_pct": self.fee_pct,
                    "slippage_pct": self.slippage_pct,
                    "bars_count": len(df),
                    "timestamp": datetime.now().isoformat(),
                }

                self.progress.emit("Complete")
                self.finished.emit(result_dict)

            finally:
                if self.disabled_models:
                    settings.set("disabled_models", old_disabled)

        except Exception as e:
            logger.exception("Backtest worker error")
            self.error.emit(f"Backtest failed: {str(e)}")


# ── KPI Card Widget ───────────────────────────────────────────
class KPICard(QFrame):
    """Single KPI metric card (title, value, color-coded)."""

    def __init__(self, title: str, value: str, unit: str = "", is_negative: bool = False, parent=None):
        super().__init__(parent)
        self.setStyleSheet(_CARD_STYLE)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(4)

        # Title
        lbl_title = QLabel(title)
        lbl_title.setStyleSheet(f"color: {_C_MUTED}; font-size: 11px; font-weight: bold;")

        # Value
        lbl_value = QLabel(value)
        color = _C_BEAR if is_negative else _C_BULL
        lbl_value.setStyleSheet(f"color: {color}; font-size: 18px; font-weight: bold;")

        # Unit
        lbl_unit = QLabel(unit)
        lbl_unit.setStyleSheet(f"color: {_C_MUTED}; font-size: 10px;")

        layout.addWidget(lbl_title)
        layout.addWidget(lbl_value)
        if unit:
            layout.addWidget(lbl_unit)
        layout.addStretch()


# ── Main Backtesting Page ─────────────────────────────────────
class BacktestingPage(QWidget):
    """Professional IDSS strategy evaluation interface."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("backtesting_page")
        self.setStyleSheet(f"QWidget#backtesting_page {{ background-color: {_C_BG}; }}")

        self._worker: Optional[IDSSBacktestWorker] = None
        self._current_result: Optional[Dict] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header
        layout.addWidget(PageHeader(
            title="IDSS Backtest",
            subtitle="Strategy evaluation & historical simulation"
        ))

        # Main content
        main = QWidget()
        main_layout = QVBoxLayout(main)
        main_layout.setContentsMargins(24, 24, 24, 24)
        main_layout.setSpacing(16)

        # ── Config Panel ──────────────────────────────────────
        main_layout.addWidget(self._build_config_panel())

        # ── Splitter: Results (top) + Saved Results (bottom) ──
        splitter = QSplitter(Qt.Vertical)
        splitter.setStyleSheet(f"QSplitter::handle {{ background-color: {_C_BORDER}; }}")

        results_widget = self._build_results_panel()
        saved_widget = self._build_saved_results_panel()

        splitter.addWidget(results_widget)
        splitter.addWidget(saved_widget)
        splitter.setSizes([600, 200])

        main_layout.addWidget(splitter, 1)

        scroll = QScrollArea()
        scroll.setWidget(main)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(f"QScrollArea {{ background-color: {_C_BG}; border: none; }}")
        layout.addWidget(scroll, 1)

    # ──────────────────────────────────────────────────────────
    # Config Panel
    # ──────────────────────────────────────────────────────────

    def _build_config_panel(self) -> QFrame:
        """Build the configuration input panel."""
        frame = QFrame()
        frame.setStyleSheet(_CARD_STYLE)
        layout = QVBoxLayout(frame)
        layout.setSpacing(12)

        # Row 1: Data Configuration
        row1 = QHBoxLayout()
        row1.setSpacing(12)

        # Symbol
        lbl_sym = QLabel("Symbol")
        lbl_sym.setStyleSheet(f"color: {_C_MUTED}; font-size: 11px; font-weight: bold;")
        self.combo_symbol = QComboBox()
        self.combo_symbol.setStyleSheet(_COMBO)
        self.combo_symbol.addItems([
            "BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT", "SOL/USDT",
            "TRX/USDT", "DOGE/USDT", "ADA/USDT", "BCH/USDT", "HYPE/USDT",
            "LINK/USDT", "XLM/USDT", "AVAX/USDT", "HBAR/USDT", "SUI/USDT",
            "NEAR/USDT", "ICP/USDT", "ONDO/USDT", "ALGO/USDT", "RENDER/USDT",
        ])
        row1.addLayout(self._form_col(lbl_sym, self.combo_symbol))

        # Timeframe
        lbl_tf = QLabel("Timeframe")
        lbl_tf.setStyleSheet(f"color: {_C_MUTED}; font-size: 11px; font-weight: bold;")
        self.combo_timeframe = QComboBox()
        self.combo_timeframe.setStyleSheet(_COMBO)
        self.combo_timeframe.addItems(["1h", "4h", "1d"])
        self.combo_timeframe.setCurrentText("1h")
        row1.addLayout(self._form_col(lbl_tf, self.combo_timeframe))

        # Start Date
        lbl_start = QLabel("Start Date")
        lbl_start.setStyleSheet(f"color: {_C_MUTED}; font-size: 11px; font-weight: bold;")
        self.date_start = QDateEdit()
        self.date_start.setStyleSheet(_DATE)
        self.date_start.setDate(QDate.currentDate().addYears(-1))
        self.date_start.setCalendarPopup(True)
        row1.addLayout(self._form_col(lbl_start, self.date_start))

        # End Date
        lbl_end = QLabel("End Date")
        lbl_end.setStyleSheet(f"color: {_C_MUTED}; font-size: 11px; font-weight: bold;")
        self.date_end = QDateEdit()
        self.date_end.setStyleSheet(_DATE)
        self.date_end.setDate(QDate.currentDate())
        self.date_end.setCalendarPopup(True)
        row1.addLayout(self._form_col(lbl_end, self.date_end))

        # Capital
        lbl_capital = QLabel("Capital (USDT)")
        lbl_capital.setStyleSheet(f"color: {_C_MUTED}; font-size: 11px; font-weight: bold;")
        self.spin_capital = QDoubleSpinBox()
        self.spin_capital.setStyleSheet(_SPIN)
        self.spin_capital.setRange(100, 10_000_000)
        self.spin_capital.setValue(100_000)
        self.spin_capital.setSingleStep(1_000)
        row1.addLayout(self._form_col(lbl_capital, self.spin_capital))

        # Fee %
        lbl_fee = QLabel("Fee (%)")
        lbl_fee.setStyleSheet(f"color: {_C_MUTED}; font-size: 11px; font-weight: bold;")
        self.spin_fee = QDoubleSpinBox()
        self.spin_fee.setStyleSheet(_SPIN)
        self.spin_fee.setRange(0.0, 2.0)
        self.spin_fee.setValue(0.10)
        self.spin_fee.setSingleStep(0.01)
        self.spin_fee.setDecimals(3)
        row1.addLayout(self._form_col(lbl_fee, self.spin_fee))

        # Slippage %
        lbl_slip = QLabel("Slippage (%)")
        lbl_slip.setStyleSheet(f"color: {_C_MUTED}; font-size: 11px; font-weight: bold;")
        self.spin_slippage = QDoubleSpinBox()
        self.spin_slippage.setStyleSheet(_SPIN)
        self.spin_slippage.setRange(0.0, 1.0)
        self.spin_slippage.setValue(0.05)
        self.spin_slippage.setSingleStep(0.01)
        self.spin_slippage.setDecimals(3)
        row1.addLayout(self._form_col(lbl_slip, self.spin_slippage))

        layout.addLayout(row1)

        # Row 2: Strategy Controls
        row2 = QHBoxLayout()
        row2.setSpacing(12)

        # Confluence Threshold
        lbl_conf = QLabel("Confluence Threshold")
        lbl_conf.setStyleSheet(f"color: {_C_MUTED}; font-size: 11px; font-weight: bold;")
        self.spin_threshold = QDoubleSpinBox()
        self.spin_threshold.setStyleSheet(_SPIN)
        self.spin_threshold.setRange(0.20, 0.90)
        self.spin_threshold.setValue(0.45)
        self.spin_threshold.setSingleStep(0.05)
        self.spin_threshold.setDecimals(2)
        row2.addLayout(self._form_col(lbl_conf, self.spin_threshold))

        # Disabled Models
        lbl_disabled = QLabel("Disabled Models")
        lbl_disabled.setStyleSheet(f"color: {_C_MUTED}; font-size: 11px; font-weight: bold;")

        models_frame = QFrame()
        models_layout = QHBoxLayout(models_frame)
        models_layout.setContentsMargins(0, 0, 0, 0)
        models_layout.setSpacing(12)

        from core.strategies.strategy_registry import STRATEGY_REGISTRY
        self.model_checkboxes = {}
        for model_def in STRATEGY_REGISTRY:
            cb = QCheckBox(model_def.name)
            cb.setStyleSheet(_CHECKBOX)
            self.model_checkboxes[model_def.name] = cb
            models_layout.addWidget(cb)

        # Load current disabled models from config
        try:
            from config.settings import settings
            disabled = settings.get("disabled_models", [])
            for model_name, cb in self.model_checkboxes.items():
                cb.setChecked(model_name in disabled)
        except Exception:
            pass

        row2.addWidget(lbl_disabled)
        row2.addWidget(models_frame, 1)

        layout.addLayout(row2)

        # Row 3: Run Controls
        row3 = QHBoxLayout()
        row3.setSpacing(12)

        self.btn_fetch = QPushButton("Fetch Data")
        self.btn_fetch.setStyleSheet(_BTN_SECONDARY)
        self.btn_fetch.clicked.connect(self._on_fetch_data)
        row3.addWidget(self.btn_fetch)

        self.btn_run = QPushButton("Run IDSS Backtest")
        self.btn_run.setStyleSheet(_BTN_PRIMARY)
        self.btn_run.clicked.connect(self._on_run_backtest)
        row3.addWidget(self.btn_run)

        self.progress_bar = QProgressBar()
        self.progress_bar.setStyleSheet(
            f"QProgressBar {{ background-color: {_C_CARD}; border: 1px solid {_C_BORDER}; "
            f"border-radius: 4px; height: 18px; text-align: center; }}"
            f"QProgressBar::chunk {{ background-color: {_C_BLUE}; }}"
        )
        self.progress_bar.setVisible(False)
        row3.addWidget(self.progress_bar, 1)

        self.lbl_status = QLabel("Ready")
        self.lbl_status.setStyleSheet(f"color: {_C_MUTED}; font-size: 11px;")
        row3.addWidget(self.lbl_status)

        layout.addLayout(row3)

        return frame

    # ──────────────────────────────────────────────────────────
    # Results Panel
    # ──────────────────────────────────────────────────────────

    def _build_results_panel(self) -> QWidget:
        """Build the results display panel (Summary, Equity Curve, Trade Log)."""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)

        self.tabs = QTabWidget()
        self.tabs.setStyleSheet(
            f"QTabWidget {{ background-color: {_C_BG}; border: none; }}"
            f"QTabBar::tab {{ background-color: {_C_BORDER}; color: {_C_MUTED}; padding: 8px 16px; "
            f"border-radius: 4px 4px 0 0; margin-right: 2px; }}"
            f"QTabBar::tab:selected {{ background-color: {_C_CARD}; color: {_C_TEXT}; }}"
        )

        # Tab 1: Summary
        self.tab_summary = self._build_summary_tab()
        self.tabs.addTab(self.tab_summary, "Summary")

        # Tab 2: Equity Curve
        self.tab_equity = self._build_equity_tab()
        self.tabs.addTab(self.tab_equity, "Equity Curve")

        # Tab 3: Trade Log
        self.tab_trades = self._build_trades_tab()
        self.tabs.addTab(self.tab_trades, "Trade Log")

        layout.addWidget(self.tabs)
        return widget

    def _build_summary_tab(self) -> QWidget:
        """Build KPI summary tab."""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        # Grid of KPI cards
        grid = QGridLayout()
        grid.setSpacing(12)

        self.kpi_cards = {
            "net_profit": KPICard("Net Profit", "—", "USDT"),
            "total_return": KPICard("Total Return", "—", "%"),
            "win_rate": KPICard("Win Rate", "—", "%"),
            "profit_factor": KPICard("Profit Factor", "—"),
            "expectancy": KPICard("Expectancy", "—", "R"),
            "avg_rr": KPICard("Avg R:R", "—"),
            "max_dd": KPICard("Max Drawdown", "—", "%", is_negative=True),
            "sharpe": KPICard("Sharpe Ratio", "—"),
            "total_trades": KPICard("Total Trades", "—"),
            "avg_win": KPICard("Avg Win", "—", "USDT"),
            "avg_loss": KPICard("Avg Loss", "—", "USDT", is_negative=True),
            "sortino": KPICard("Sortino Ratio", "—"),
            "long_wr": KPICard("Long Win Rate", "—", "%"),
            "short_wr": KPICard("Short Win Rate", "—", "%"),
            "exposure": KPICard("Exposure", "—", "%"),
            "max_consec_loss": KPICard("Max Consec Losses", "—"),
        }

        positions = [
            ("net_profit", 0, 0), ("total_return", 0, 1), ("win_rate", 0, 2), ("profit_factor", 0, 3),
            ("expectancy", 1, 0), ("avg_rr", 1, 1), ("max_dd", 1, 2), ("sharpe", 1, 3),
            ("total_trades", 2, 0), ("avg_win", 2, 1), ("avg_loss", 2, 2), ("sortino", 2, 3),
            ("long_wr", 3, 0), ("short_wr", 3, 1), ("exposure", 3, 2), ("max_consec_loss", 3, 3),
        ]

        for key, row, col in positions:
            grid.addWidget(self.kpi_cards[key], row, col)

        layout.addLayout(grid, 0)
        layout.addStretch()

        return widget

    def _build_equity_tab(self) -> QWidget:
        """Build equity curve chart tab."""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(8, 8, 8, 8)

        if _PG_AVAILABLE:
            # PyQtGraph plot
            self.plot_equity = pg.PlotWidget(
                title="Equity Curve",
                axisItems={"bottom": _DateAxisItem(orientation="bottom")},
            )
            self.plot_equity.setLabel("left", "Equity", units="USDT")
            self.plot_equity.setLabel("bottom", "Date")
            self.plot_equity.setStyleSheet(f"background-color: {_C_SHADOW};")

            # Customize appearance
            self.plot_equity.getPlotItem().getAxis("left").setPen(pg.mkPen(color=_C_MUTED, width=1))
            self.plot_equity.getPlotItem().getAxis("bottom").setPen(pg.mkPen(color=_C_MUTED, width=1))
            self.plot_equity.getPlotItem().getAxis("left").setTextPen(pg.mkPen(color=_C_MUTED))
            self.plot_equity.getPlotItem().getAxis("bottom").setTextPen(pg.mkPen(color=_C_MUTED))
            layout.addWidget(self.plot_equity)
        else:
            self.plot_equity = None
            placeholder = QLabel("Install pyqtgraph to view equity curves:\npip install pyqtgraph")
            placeholder.setStyleSheet(f"color: {_C_MUTED}; font-size: 14px; padding: 40px;")
            placeholder.setAlignment(Qt.AlignCenter)
            layout.addWidget(placeholder)

        return widget

    def _build_trades_tab(self) -> QWidget:
        """Build trade log table tab."""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)

        self.table_trades = QTableWidget()
        self.table_trades.setColumnCount(13)
        self.table_trades.setHorizontalHeaderLabels([
            "Entry Time", "Exit Time", "Side", "Entry Price", "Exit Price",
            "P&L $", "P&L %", "R-Multiple", "Regime", "Models", "Score", "Duration", "Exit Reason"
        ])
        self.table_trades.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table_trades.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table_trades.setAlternatingRowColors(True)
        self.table_trades.setStyleSheet(
            f"QTableWidget {{ background-color: {_C_CARD}; gridline-color: {_C_BORDER}; }}"
            f"QHeaderView::section {{ background-color: {_C_BORDER}; color: {_C_TEXT}; padding: 4px; }}"
            f"QTableWidget::item {{ padding: 4px; }}"
        )

        layout.addWidget(self.table_trades)
        return widget

    # ──────────────────────────────────────────────────────────
    # Saved Results Panel
    # ──────────────────────────────────────────────────────────

    def _build_saved_results_panel(self) -> QFrame:
        """Build saved results management panel."""
        frame = QFrame()
        frame.setStyleSheet(_CARD_STYLE)
        layout = QVBoxLayout(frame)
        layout.setSpacing(8)

        # Controls row
        controls = QHBoxLayout()
        controls.setSpacing(8)

        self.input_result_name = QLineEdit()
        self.input_result_name.setStyleSheet(
            f"QLineEdit {{ background-color: {_C_CARD}; color: {_C_TEXT}; "
            f"border: 1px solid {_C_BORDER}; border-radius: 4px; padding: 4px 8px; }}"
        )
        self.input_result_name.setPlaceholderText("Result name (auto-generated if empty)")
        controls.addWidget(self.input_result_name, 1)

        self.btn_save = QPushButton("Save Result")
        self.btn_save.setStyleSheet(_BTN_SECONDARY)
        self.btn_save.clicked.connect(self._on_save_result)
        controls.addWidget(self.btn_save)

        self.btn_compare = QPushButton("Compare Selected")
        self.btn_compare.setStyleSheet(_BTN_SECONDARY)
        self.btn_compare.clicked.connect(self._on_compare_results)
        controls.addWidget(self.btn_compare)

        self.btn_export = QPushButton("Export CSV")
        self.btn_export.setStyleSheet(_BTN_SECONDARY)
        self.btn_export.clicked.connect(self._on_export_csv)
        controls.addWidget(self.btn_export)

        layout.addLayout(controls)

        # Results table
        self.table_results = QTableWidget()
        self.table_results.setColumnCount(10)
        self.table_results.setHorizontalHeaderLabels([
            "✓", "Name", "Date Run", "Symbol", "TF", "Period", "Trades", "Return %", "WR %", "PF"
        ])
        self.table_results.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table_results.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table_results.setMaximumHeight(120)
        self.table_results.setStyleSheet(
            f"QTableWidget {{ background-color: {_C_CARD}; gridline-color: {_C_BORDER}; }}"
            f"QHeaderView::section {{ background-color: {_C_BORDER}; color: {_C_TEXT}; padding: 4px; }}"
        )

        layout.addWidget(self.table_results)

    # ──────────────────────────────────────────────────────────
    # Event Handlers
    # ──────────────────────────────────────────────────────────

    @Slot()
    def _on_fetch_data(self):
        """Fetch historical data (preview only)."""
        QMessageBox.information(
            self,
            "Data Fetch",
            "Historical data fetching is integrated into backtest runs.\n\n"
            "Click 'Run IDSS Backtest' to fetch and backtest simultaneously."
        )

    @Slot()
    def _on_run_backtest(self):
        """Run the IDSS backtest."""
        if self._worker and self._worker.isRunning():
            QMessageBox.warning(self, "In Progress", "Backtest already running.")
            return

        # Validate inputs
        if self.date_start.date() >= self.date_end.date():
            QMessageBox.warning(self, "Invalid Date Range", "End date must be after start date.")
            return

        if self.spin_capital.value() <= 0:
            QMessageBox.warning(self, "Invalid Capital", "Capital must be greater than zero.")
            return

        # Collect disabled models
        disabled = [name for name, cb in self.model_checkboxes.items() if cb.isChecked()]

        # Start worker
        self._worker = IDSSBacktestWorker(
            symbol=self.combo_symbol.currentText(),
            timeframe=self.combo_timeframe.currentText(),
            start_date=self.date_start.date().toString(Qt.ISODate),
            end_date=self.date_end.date().toString(Qt.ISODate),
            initial_capital=self.spin_capital.value(),
            fee_pct=self.spin_fee.value(),
            slippage_pct=self.spin_slippage.value(),
            disabled_models=disabled,
            confluence_threshold=self.spin_threshold.value(),
        )
        self._worker.progress.connect(self._on_worker_progress)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.error.connect(self._on_worker_error)

        self.btn_run.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        self._worker.start()

    @Slot(str)
    def _on_worker_progress(self, msg: str):
        """Update progress status."""
        self.lbl_status.setText(msg)

    @Slot(dict)
    def _on_worker_finished(self, result: dict):
        """Process completed backtest."""
        self._current_result = result
        self.btn_run.setEnabled(True)
        self.progress_bar.setVisible(False)
        self.lbl_status.setText("Complete")

        try:
            # Update KPI cards
            self._render_kpis(result)

            # Plot equity curve
            self._render_equity_curve(result)

            # Populate trade log
            self._render_trade_log(result)

            # Enable save button
            self.btn_save.setEnabled(True)
        except Exception as e:
            logger.exception("Error rendering results")
            QMessageBox.critical(self, "Render Error", f"Failed to display results: {str(e)}")

    @Slot(str)
    def _on_worker_error(self, msg: str):
        """Handle worker error."""
        self.btn_run.setEnabled(True)
        self.progress_bar.setVisible(False)
        QMessageBox.critical(self, "Backtest Error", msg)

    @Slot()
    def _on_save_result(self):
        """Save the current result."""
        if not self._current_result:
            QMessageBox.warning(self, "No Result", "Run a backtest first.")
            return

        # Generate name if empty
        name = self.input_result_name.text().strip()
        if not name:
            symbol = self._current_result.get("symbol", "BTC")
            tf = self._current_result.get("timeframe", "1h")
            trades = len(self._current_result.get("trades", []))
            name = f"{symbol}_{tf}_{trades}t_{datetime.now().strftime('%Y%m%d_%H%M')}"

        try:
            from core.backtesting.result_store import BacktestResultStore
            store = BacktestResultStore()
            store.save_result(name, self._current_result)
            self.input_result_name.clear()
            QMessageBox.information(self, "Saved", f"Result saved as '{name}'")
        except Exception as e:
            QMessageBox.critical(self, "Save Error", str(e))

    @Slot()
    def _on_compare_results(self):
        """Compare selected saved results."""
        QMessageBox.information(
            self,
            "Compare Results",
            "Comparison feature coming soon.\n\n"
            "Select multiple results in the table above and click this button."
        )

    @Slot()
    def _on_export_csv(self):
        """Export trade log to CSV."""
        if not self._current_result:
            QMessageBox.warning(self, "No Result", "Run a backtest first.")
            return

        try:
            trades = self._current_result.get("trades", [])
            if not trades:
                QMessageBox.information(self, "No Trades", "No trades to export.")
                return

            df = pd.DataFrame(trades)
            filename = (
                f"{self._current_result['symbol']}_"
                f"{self._current_result['timeframe']}_"
                f"trades_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            )
            df.to_csv(filename, index=False)
            QMessageBox.information(self, "Exported", f"Trades exported to {filename}")
        except Exception as e:
            QMessageBox.critical(self, "Export Error", str(e))

    # ──────────────────────────────────────────────────────────
    # Render Methods
    # ──────────────────────────────────────────────────────────

    def _render_kpis(self, result: dict):
        """Populate KPI cards from result."""
        kpis = result.get("kpis", {})

        # Helper to format numbers
        def fmt(value, decimals=2, suffix=""):
            if value is None:
                return "—"
            if isinstance(value, float):
                return f"{value:.{decimals}f}{suffix}"
            return str(value)

        # Update each card
        updates = {
            "net_profit": (f"${kpis.get('net_profit_usdt', 0):,.2f}", "USDT", kpis.get('net_profit_usdt', 0) < 0),
            "total_return": (f"{kpis.get('total_return_pct', 0):.2f}%", "", kpis.get('total_return_pct', 0) < 0),
            "win_rate": (f"{kpis.get('win_rate', 0) * 100:.1f}%", "", False),
            "profit_factor": (f"{kpis.get('profit_factor', 0):.2f}", "", kpis.get('profit_factor', 0) < 1.0),
            "expectancy": (f"{kpis.get('expectancy_r', 0):.3f}", "R", kpis.get('expectancy_r', 0) < 0),
            "avg_rr": (f"{kpis.get('avg_rr_ratio', 0):.2f}", "", False),
            "max_dd": (f"{kpis.get('max_drawdown_pct', 0):.2f}%", "", True),
            "sharpe": (f"{kpis.get('sharpe_ratio', 0):.2f}", "", False),
            "total_trades": (str(kpis.get("total_trades", 0)), "", False),
            "avg_win": (f"${kpis.get('avg_win_usdt', 0):,.2f}", "USDT", kpis.get('avg_win_usdt', 0) < 0),
            "avg_loss": (f"${kpis.get('avg_loss_usdt', 0):,.2f}", "USDT", True),
            "sortino": (f"{kpis.get('sortino_ratio', 0):.2f}", "", False),
            "long_wr": (f"{kpis.get('long_win_rate', 0) * 100:.1f}%", "", False),
            "short_wr": (f"{kpis.get('short_win_rate', 0) * 100:.1f}%", "", False),
            "exposure": (f"{kpis.get('exposure_pct', 0):.1f}%", "", False),
            "max_consec_loss": (str(kpis.get("max_consecutive_losses", 0)), "", False),
        }

        for key, (value, unit, is_neg) in updates.items():
            card = self.kpi_cards[key]
            # Recreate the card with new values
            title = card.findChildren(QLabel)[0].text()
            card.deleteLater()
            self.kpi_cards[key] = KPICard(title, value, unit, is_negative=is_neg)
            # Re-add to grid (simplified — just update text)
            for child in self.tab_summary.findChildren(QLabel):
                if key in str(child.objectName()):
                    child.setText(value)

    def _render_equity_curve(self, result: dict):
        """Plot equity curve."""
        if not _PG_AVAILABLE or self.plot_equity is None:
            return

        equity = result.get("equity_curve", [])
        if not equity:
            return

        # Convert to numpy array
        equity_np = np.array(equity, dtype=float)

        # X-axis: bar indices converted to Unix timestamps
        bars_count = result.get("bars_count", len(equity))
        x = np.arange(len(equity_np)) * (bars_count / len(equity_np))

        # Clear and plot
        self.plot_equity.clear()
        self.plot_equity.plot(x, equity_np, pen=pg.mkPen(color=_C_BLUE, width=2), name="Equity")

        # Add drawdown shading
        running_max = np.maximum.accumulate(equity_np)
        drawdown = (running_max - equity_np) / running_max * 100
        try:
            self.plot_equity.fill_between(x, equity_np, running_max, brush=pg.mkBrush(color=_C_BEAR + "40"))
        except AttributeError:
            pass  # fill_between not available in all pyqtgraph versions

        self.plot_equity.setLabel("left", "Equity", units="USDT")

    def _render_trade_log(self, result: dict):
        """Populate trade log table."""
        trades = result.get("trades", [])
        self.table_trades.setRowCount(len(trades))

        for row, trade in enumerate(trades):
            cells = [
                str(trade.get("entry_time", "")),
                str(trade.get("exit_time", "")),
                trade.get("side", "").upper(),
                f"${trade.get('entry_price', 0):.2f}",
                f"${trade.get('exit_price', 0):.2f}",
                f"${trade.get('pnl_usdt', 0):,.2f}",
                f"{trade.get('pnl_pct', 0):.2f}%",
                f"{trade.get('r_multiple', 0):.2f}",
                trade.get("regime", "—"),
                ", ".join(trade.get("models_fired", [])),
                f"{trade.get('score', 0):.2f}",
                f"{trade.get('duration_bars', 0)}",
                trade.get("exit_reason", "—"),
            ]

            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setForeground(QBrush(QColor(_C_BULL if trade.get('pnl_usdt', 0) > 0 else _C_BEAR)))
                self.table_trades.setItem(row, col, item)

    # ──────────────────────────────────────────────────────────
    # Utility Methods
    # ──────────────────────────────────────────────────────────

    def _form_col(self, label: QLabel, widget: QWidget) -> QVBoxLayout:
        """Create a form column (label above widget)."""
        col = QVBoxLayout()
        col.setSpacing(4)
        col.addWidget(label)
        col.addWidget(widget)
        return col
