# ============================================================
# NEXUS TRADER — Strategies Page (UI Rewrite)
#
# Comprehensive strategy registry and parameter editor.
# Features:
#   • Model grid with enable/disable, weight, metrics
#   • Detail panel with per-model and global parameters
#   • Real-time metrics refresh (WR, PF, trades, P&L)
#   • Dirty tracking and multi-level save/restore workflow
#   • Validation, audit logging, config snapshots
# ============================================================
from __future__ import annotations

import logging
import json
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QHeaderView, QPushButton, QLabel, QCheckBox, QDoubleSpinBox,
    QSpinBox, QMessageBox, QScrollArea, QFrame, QTabWidget,
    QFormLayout, QGroupBox, QSplitter, QAbstractItemView, QTextEdit,
    QComboBox
)
from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QFont, QIcon

from gui.main_window import PageHeader
from config.settings import settings
from core.strategies.strategy_registry import (
    STRATEGY_REGISTRY, GLOBAL_PARAMS, ModelDef, ModelParamDef,
    get_model_def, is_model_enabled, get_model_weight, get_all_config_keys
)
from core.strategies.strategy_metrics import StrategyMetricsCalculator, ModelStats
from core.strategies.audit_logger import AuditLogger

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Shared Styles
# ─────────────────────────────────────────────────────────────
_TABLE_STYLE = (
    "QTableWidget { background:#0A0E1A; color:#E8EBF0; "
    "gridline-color:#141E2E; font-size:12px; border:none; }"
    "QTableWidget::item:selected { background:#1A2D4A; }"
    "QTableWidget::item:alternate { background:#0C1018; }"
    "QHeaderView::section { background:#0D1320; color:#8899AA; "
    "padding:6px 8px; border:none; "
    "border-bottom:1px solid #1A2332; font-size:12px; font-weight:600; }"
)

_CARD_STYLE = (
    "QFrame#card { background:#0D1320; border:1px solid #1A2332; border-radius:6px; }"
)

_BTN_SAVE = (
    "QPushButton { background:#0A4D00; color:#44FF44; border:1px solid #1A7700; "
    "border-radius:5px; font-size:12px; font-weight:600; padding:6px 12px; }"
    "QPushButton:hover { background:#105500; }"
    "QPushButton:disabled { color:#1A3A1A; border-color:#0A1A0A; background:#050505; }"
)

_BTN_NEUTRAL = (
    "QPushButton { background:#0D1320; color:#8899AA; border:1px solid #2A3A52; "
    "border-radius:5px; font-size:12px; font-weight:600; padding:6px 12px; }"
    "QPushButton:hover { background:#1A2332; color:#E8EBF0; }"
)

_BADGE_CORE = "background:#4488CC; color:white; padding:4px 8px; border-radius:3px; font-size:11px; font-weight:600;"
_BADGE_AGENT = "background:#CC7700; color:white; padding:4px 8px; border-radius:3px; font-size:11px; font-weight:600;"
_BADGE_ML = "background:#AA44CC; color:white; padding:4px 8px; border-radius:3px; font-size:11px; font-weight:600;"
_BADGE_META = "background:#008888; color:white; padding:4px 8px; border-radius:3px; font-size:11px; font-weight:600;"

_COLOR_MAP = {
    "CORE": ("#4488CC", _BADGE_CORE),
    "AGENT": ("#CC7700", _BADGE_AGENT),
    "ML": ("#AA44CC", _BADGE_ML),
    "META": ("#008888", _BADGE_META),
}


# ─────────────────────────────────────────────────────────────
# Item Creation Helpers
# ─────────────────────────────────────────────────────────────
def _ci(text: str, color: str = "#E8EBF0",
        align: Qt.AlignmentFlag = Qt.AlignCenter) -> QTableWidgetItem:
    """Create a centered, colored table item."""
    item = QTableWidgetItem(text)
    item.setForeground(QColor(color))
    item.setTextAlignment(align | Qt.AlignVCenter)
    item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
    return item


def _fmt_pct(v: float, decimals: int = 1) -> str:
    """Format as percentage."""
    if v == 0:
        return "—"
    return f"{v*100:.{decimals}f}%"


def _fmt_currency(v: float) -> str:
    """Format as USD currency."""
    if v == 0:
        return "—"
    sign = "-" if v < 0 else ""
    abs_v = abs(v)
    if abs_v >= 1000:
        return f"{sign}${abs_v:,.0f}"
    return f"{sign}${abs_v:.2f}"


def _fmt_time_ago(ts: Optional[str]) -> str:
    """Format timestamp as 'time ago'."""
    if not ts:
        return "—"
    try:
        # Parse ISO format
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = now - dt

        if delta.total_seconds() < 60:
            return "just now"
        elif delta.total_seconds() < 3600:
            minutes = int(delta.total_seconds() // 60)
            return f"{minutes}m ago"
        elif delta.total_seconds() < 86400:
            hours = int(delta.total_seconds() // 3600)
            return f"{hours}h ago"
        else:
            days = int(delta.total_seconds() // 86400)
            return f"{days}d ago"
    except Exception:
        return ts[:10] if ts else "—"


# ─────────────────────────────────────────────────────────────
# Parameter Detail Panel
# ─────────────────────────────────────────────────────────────
class DetailPanel(QWidget):
    """
    Detail panel showing parameters for selected model.
    Contains 3 tabs: Parameters, Global Parameters, Audit Log.
    """

    parameter_changed = Signal(str, Any)  # (key, new_value)

    def __init__(self, parent: StrategiesPage):
        super().__init__(parent)
        self.parent_page = parent
        self.current_model: Optional[ModelDef] = None
        self.param_widgets: Dict[str, QWidget] = {}
        self.global_param_widgets: Dict[str, QWidget] = {}
        self._affinity_widgets: Dict[str, QWidget] = {}

        # UI
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.tabs = QTabWidget()
        self.tabs.setStyleSheet(
            "QTabBar::tab { padding: 8px 16px; font-size: 12px; } "
            "QTabWidget::pane { border: 1px solid #1A2332; }"
        )

        # Tab 1: Model Parameters
        self.tab_params = self._create_scroll_area("Select a model from the grid to edit parameters.")
        self.tabs.addTab(self.tab_params, "Parameters")

        # Tab 2: Regime Affinity
        self.tab_affinity = self._create_scroll_area("Regime affinity editing will appear here.")
        self.tabs.addTab(self.tab_affinity, "Regime Affinity")

        # Tab 3: Global Parameters
        self.tab_global = self._build_global_tab()
        self.tabs.addTab(self.tab_global, "Global")

        # Tab 4: Audit Log
        self.tab_audit = QWidget()
        self._build_audit_tab()
        self.tabs.addTab(self.tab_audit, "Audit Log")

        layout.addWidget(self.tabs)
        self.setLayout(layout)

    def _create_scroll_area(self, placeholder_text: str) -> QWidget:
        """Create a scrollable parameter area."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        container = QWidget()
        container.setStyleSheet("background: transparent;")
        form_layout = QFormLayout(container)
        form_layout.setSpacing(8)
        form_layout.setContentsMargins(8, 8, 8, 8)

        # Placeholder
        label_placeholder = QLabel(placeholder_text)
        label_placeholder.setStyleSheet("color: #8899AA; font-style: italic;")
        form_layout.addRow(label_placeholder)

        scroll.setWidget(container)
        return scroll

    def _build_global_tab(self) -> QWidget:
        """Build global parameter editing tab."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        container = QWidget()
        container.setStyleSheet("background: transparent;")
        form_layout = QFormLayout(container)
        form_layout.setSpacing(8)
        form_layout.setContentsMargins(8, 8, 8, 8)

        # Organize by section
        sections: Dict[str, list] = {}
        for param in GLOBAL_PARAMS:
            section = param.section or "General"
            if section not in sections:
                sections[section] = []
            sections[section].append(param)

        for section_name in sorted(sections.keys()):
            group = QGroupBox(section_name)
            group.setStyleSheet("QGroupBox { color:#8899AA; font-weight:600; "
                               "border:1px solid #1A2332; border-radius:3px; padding-top:8px; }"
                               "QGroupBox::title { subcontrol-position: top left; padding: 0 3px; }")
            group_layout = QFormLayout(group)
            group_layout.setSpacing(8)

            for param in sections[section_name]:
                widget = self._create_param_widget(param)
                self.global_param_widgets[param.key] = widget

                # Label with default indicator
                label_text = param.label
                current_val = settings.get(param.key, param.default)
                if current_val != param.default:
                    label_text += " •"

                label = QLabel(label_text)
                label.setStyleSheet("color:#E8EBF0; font-size:12px;")

                group_layout.addRow(label, widget)

            form_layout.addRow(group)

        scroll.setWidget(container)
        return scroll

    def _build_audit_tab(self):
        """Build audit log tab."""
        layout = QVBoxLayout(self.tab_audit)
        layout.setSpacing(8)
        layout.setContentsMargins(8, 8, 8, 8)

        self.audit_text = QTextEdit()
        self.audit_text.setReadOnly(True)
        self.audit_text.setStyleSheet(
            "QTextEdit { background:#0A0E1A; color:#8899AA; "
            "border:1px solid #1A2332; padding:8px; font-family:Courier; font-size:11px; }"
        )
        layout.addWidget(self.audit_text)

    def load_model(self, model_def: ModelDef):
        """
        Load a model's parameters into the detail panel.

        Parameters
        ----------
        model_def : ModelDef
            Model definition to load
        """
        self.current_model = model_def
        self.param_widgets.clear()

        # Tab 1: Model Parameters
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        container = QWidget()
        container.setStyleSheet("background: transparent;")
        form_layout = QFormLayout(container)
        form_layout.setSpacing(8)
        form_layout.setContentsMargins(8, 8, 8, 8)

        # Organize by section
        sections: Dict[str, list] = {}
        for param in model_def.params:
            section = param.section or "General"
            if section not in sections:
                sections[section] = []
            sections[section].append(param)

        for section_name in sorted(sections.keys()):
            if len(sections[section_name]) > 0:
                group = QGroupBox(section_name)
                group.setStyleSheet("QGroupBox { color:#8899AA; font-weight:600; "
                                   "border:1px solid #1A2332; border-radius:3px; padding-top:8px; }"
                                   "QGroupBox::title { subcontrol-position: top left; padding: 0 3px; }")
                group_layout = QFormLayout(group)
                group_layout.setSpacing(8)

                for param in sections[section_name]:
                    widget = self._create_param_widget(param)
                    self.param_widgets[param.key] = widget

                    # Label with default indicator
                    label_text = param.label
                    current_val = settings.get(param.key, param.default)
                    if current_val != param.default:
                        label_text += " •"

                    label = QLabel(label_text)
                    label.setStyleSheet("color:#E8EBF0; font-size:12px;")

                    group_layout.addRow(label, widget)

                form_layout.addRow(group)

        # Restore defaults button
        btn_restore = QPushButton("Restore Defaults for Model")
        btn_restore.setStyleSheet(_BTN_NEUTRAL)
        btn_restore.clicked.connect(self._restore_model_defaults)
        form_layout.addRow(btn_restore)

        scroll.setWidget(container)

        # Replace tab
        self.tabs.removeTab(0)
        self.tabs.insertTab(0, scroll, "Parameters")

        # Tab 2: Regime Affinity tab
        self._build_affinity_tab(model_def.name)

        # Tab 4: Audit log
        self._refresh_audit_log(model_def.name)

    def _create_param_widget(self, param: ModelParamDef) -> QWidget:
        """Create appropriate widget for parameter type."""
        current_val = settings.get(param.key, param.default)

        if param.param_type == "bool":
            widget = QCheckBox()
            widget.setChecked(bool(current_val))
            widget.stateChanged.connect(
                lambda: self.parameter_changed.emit(param.key, widget.isChecked())
            )
        elif param.param_type == "int":
            widget = QSpinBox()
            widget.setMinimum(int(param.min_val or -999999))
            widget.setMaximum(int(param.max_val or 999999))
            if param.step:
                widget.setSingleStep(int(param.step))
            widget.setValue(int(current_val))
            widget.valueChanged.connect(
                lambda v: self.parameter_changed.emit(param.key, v)
            )
        elif param.param_type == "float":
            widget = QDoubleSpinBox()
            widget.setMinimum(float(param.min_val or -999999.0))
            widget.setMaximum(float(param.max_val or 999999.0))
            if param.step:
                widget.setSingleStep(float(param.step))
            widget.setDecimals(4)
            widget.setValue(float(current_val))
            widget.valueChanged.connect(
                lambda v: self.parameter_changed.emit(param.key, v)
            )
        elif param.param_type == "enum":
            widget = QComboBox()
            for value in (param.enum_values or []):
                widget.addItem(value)
            if str(current_val) in (param.enum_values or []):
                widget.setCurrentText(str(current_val))
            widget.currentTextChanged.connect(
                lambda v: self.parameter_changed.emit(param.key, v)
            )
        else:
            widget = QLabel(str(current_val))

        # Tooltip
        if hasattr(widget, 'setToolTip'):
            widget.setToolTip(param.description or "")

        return widget

    def _refresh_audit_log(self, model_name: str):
        """Load and display audit log entries for a model."""
        try:
            audit_logger = AuditLogger()
            # Try to load entries — may not exist yet
            text = ""
            try:
                with open(audit_logger.log_file, 'r') as f:
                    lines = f.readlines()
                    entries = []
                    for line in lines:
                        try:
                            entry = json.loads(line.strip())
                            if entry.get("model") == model_name:
                                entries.append(entry)
                        except json.JSONDecodeError:
                            pass

                    for entry in reversed(entries[-50:]):  # Last 50, newest first
                        ts = entry.get("ts", "")[:19]
                        action = entry.get("action", "")
                        key = entry.get("key", "")
                        old_val = entry.get("old", "—")
                        new_val = entry.get("new", "—")

                        text += f"[{ts}] {action}: {key}\n"
                        text += f"  {old_val} → {new_val}\n\n"

                if not text:
                    text = "No audit entries for this model."
            except FileNotFoundError:
                text = "No audit log file yet."

            self.audit_text.setText(text)
        except Exception as e:
            self.audit_text.setText(f"Error loading audit log: {e}")

    def _restore_model_defaults(self):
        """Restore all parameters for current model to defaults."""
        if not self.current_model:
            return

        reply = QMessageBox.question(
            self, "Restore Defaults",
            f"Reset all parameters for {self.current_model.display_name} to defaults?\n\n"
            "You must Save to persist changes.",
            QMessageBox.Yes | QMessageBox.No
        )

        if reply == QMessageBox.Yes:
            for param in self.current_model.params:
                widget = self.param_widgets.get(param.key)
                if widget:
                    self._set_widget_value(widget, param.default)
                    # Emit change signal
                    self.parameter_changed.emit(param.key, param.default)

    def _build_affinity_tab(self, model_name: str):
        """Build regime affinity editing tab for a model."""
        from core.meta_decision.confluence_scorer import REGIME_AFFINITY

        layout = QVBoxLayout()
        layout.setContentsMargins(8, 8, 8, 8)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        container = QWidget()
        container.setStyleSheet("background: transparent;")
        form = QFormLayout(container)
        form.setSpacing(6)

        defaults = REGIME_AFFINITY.get(model_name, {})
        regimes = ["bull_trend", "bear_trend", "ranging", "volatility_expansion",
                   "volatility_compression", "uncertain", "crisis", "liquidation_cascade",
                   "squeeze", "recovery", "accumulation", "distribution"]

        self._affinity_widgets.clear()

        for regime in regimes:
            key = f"regime_affinity.{model_name}.{regime}"
            default_val = defaults.get(regime, 0.3)
            current_val = float(settings.get(key, default_val))

            row = QHBoxLayout()
            spin = QDoubleSpinBox()
            spin.setMinimum(0.0)
            spin.setMaximum(1.0)
            spin.setSingleStep(0.05)
            spin.setDecimals(2)
            spin.setValue(current_val)
            spin.setFixedWidth(80)
            spin.valueChanged.connect(
                lambda v, k=key: self.parameter_changed.emit(k, v)
            )

            default_label = QLabel(f"  Default: {default_val:.2f}")
            default_label.setStyleSheet("color:#667788; font-size:11px;")

            # Color indicator based on value
            if current_val != default_val:
                modified_dot = QLabel(" •")
                modified_dot.setStyleSheet("color:#CC7700; font-weight:bold; font-size:14px;")
                row.addWidget(modified_dot)

            row.addWidget(spin)
            row.addWidget(default_label)
            row.addStretch()

            self._affinity_widgets[key] = spin

            # Human-readable regime name
            display_regime = regime.replace("_", " ").title()
            label = QLabel(display_regime)
            label.setStyleSheet("color:#E8EBF0; font-size:12px; min-width:160px;")

            row_widget = QWidget()
            row_widget.setLayout(row)
            form.addRow(label, row_widget)

        # Restore defaults button
        btn = QPushButton("Restore Affinity Defaults")
        btn.setStyleSheet("QPushButton { background:#2A3040; color:#AABBCC; border:1px solid #3A4050; "
                          "border-radius:3px; padding:6px 16px; }"
                          "QPushButton:hover { background:#3A4050; }")
        btn.clicked.connect(lambda: self._restore_affinity_defaults(model_name))
        form.addRow(btn)

        scroll.setWidget(container)
        layout.addWidget(scroll)

        # Replace the affinity tab content
        new_tab = QWidget()
        new_tab.setLayout(layout)

        # Find and replace tab
        idx = -1
        for i in range(self.tabs.count()):
            if self.tabs.tabText(i) == "Regime Affinity":
                idx = i
                break
        if idx >= 0:
            self.tabs.removeTab(idx)
            self.tabs.insertTab(idx, new_tab, "Regime Affinity")
        self.tab_affinity = new_tab

    def _restore_affinity_defaults(self, model_name: str):
        """Restore regime affinity to hardcoded defaults."""
        from core.meta_decision.confluence_scorer import REGIME_AFFINITY
        defaults = REGIME_AFFINITY.get(model_name, {})

        reply = QMessageBox.question(
            self, "Restore Affinity Defaults",
            f"Reset regime affinity for {model_name} to code defaults?\nYou must Save to persist.",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return

        for key, spin in self._affinity_widgets.items():
            regime = key.split(".")[-1]
            default_val = defaults.get(regime, 0.3)
            spin.blockSignals(True)
            spin.setValue(default_val)
            spin.blockSignals(False)
            self.parameter_changed.emit(key, default_val)

    def _set_widget_value(self, widget: QWidget, value: Any):
        """Set widget value without triggering signals."""
        if isinstance(widget, QCheckBox):
            widget.blockSignals(True)
            widget.setChecked(bool(value))
            widget.blockSignals(False)
        elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
            widget.blockSignals(True)
            widget.setValue(value)
            widget.blockSignals(False)
        elif isinstance(widget, QComboBox):
            widget.blockSignals(True)
            if str(value) in [widget.itemText(i) for i in range(widget.count())]:
                widget.setCurrentText(str(value))
            widget.blockSignals(False)


# ─────────────────────────────────────────────────────────────
# Main Strategies Page
# ─────────────────────────────────────────────────────────────
class StrategiesPage(QWidget):
    """
    Main Strategies page — registry and parameter editor.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("StrategiesPage")

        self._dirty: Dict[str, Tuple[Any, Any]] = {}  # {key: (old, new)}
        self._metrics: Dict[str, ModelStats] = {}
        self._metrics_calculator = StrategyMetricsCalculator()
        self._audit_logger = AuditLogger()
        self._type_filter: Optional[str] = None

        self._build_ui()
        self._load_metrics()
        self._populate_grid()
        self._start_metrics_timer()

    def _build_ui(self):
        """Build the UI layout."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        # Header
        header = PageHeader("IDSS Strategy Registry", "Manage signal models, parameters, and weights")
        layout.addWidget(header)

        # Control bar
        control_bar = QHBoxLayout()
        control_bar.setSpacing(8)
        control_bar.setContentsMargins(0, 0, 0, 0)

        # Type filter buttons
        self.btn_filter_all = QPushButton("All")
        self.btn_filter_all.setCheckable(True)
        self.btn_filter_all.setChecked(True)
        self.btn_filter_all.setStyleSheet(_BTN_NEUTRAL)
        self.btn_filter_all.clicked.connect(lambda: self._apply_type_filter(None))
        control_bar.addWidget(self.btn_filter_all)

        self.filter_buttons: Dict[str, QPushButton] = {}
        for mtype in ("CORE", "AGENT", "ML", "META"):
            btn = QPushButton(mtype)
            btn.setCheckable(True)
            btn.setStyleSheet(_BTN_NEUTRAL)
            btn.setMaximumWidth(80)
            btn.clicked.connect(lambda checked, t=mtype: self._apply_type_filter(t if checked else None))
            self.filter_buttons[mtype] = btn
            control_bar.addWidget(btn)

        control_bar.addStretch()

        # Dirty indicator
        self.label_dirty = QLabel("")
        self.label_dirty.setStyleSheet("color:#CC8800; font-weight:600; font-size:12px;")
        control_bar.addWidget(self.label_dirty)

        # Save button
        self.btn_save = QPushButton("Save All Changes")
        self.btn_save.setStyleSheet(_BTN_SAVE)
        self.btn_save.setEnabled(False)
        self.btn_save.setMaximumWidth(150)
        self.btn_save.clicked.connect(self._on_save_all)
        control_bar.addWidget(self.btn_save)

        # Restore button
        self.btn_restore = QPushButton("Restore All Defaults")
        self.btn_restore.setStyleSheet(_BTN_NEUTRAL)
        self.btn_restore.setMaximumWidth(150)
        self.btn_restore.clicked.connect(self._on_restore_all)
        control_bar.addWidget(self.btn_restore)

        layout.addLayout(control_bar)

        # Grid and detail panel in splitter
        splitter = QSplitter(Qt.Vertical)
        splitter.setStyleSheet("QSplitter::handle { background: #1A2332; height: 4px; }")

        # Strategy grid
        self.grid = QTableWidget()
        self.grid.setColumnCount(10)
        self.grid.setHorizontalHeaderLabels([
            "Enable", "Strategy", "Type", "Weight", "Status",
            "Win Rate", "Profit Factor", "Trades", "P&L", "Last Signal"
        ])
        self.grid.setStyleSheet(_TABLE_STYLE)
        self.grid.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.grid.setSelectionMode(QAbstractItemView.SingleSelection)
        self.grid.setAlternatingRowColors(True)
        self.grid.setColumnWidth(0, 50)
        self.grid.setColumnWidth(1, 160)
        self.grid.setColumnWidth(2, 70)
        self.grid.setColumnWidth(3, 120)
        self.grid.setColumnWidth(4, 80)
        self.grid.setColumnWidth(5, 90)
        self.grid.setColumnWidth(6, 100)
        self.grid.setColumnWidth(7, 60)
        self.grid.setColumnWidth(8, 90)
        self.grid.setColumnWidth(9, 150)

        header = self.grid.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(9, QHeaderView.Stretch)
        header.setSectionResizeMode(QHeaderView.Interactive)

        self.grid.verticalHeader().setDefaultSectionSize(32)
        self.grid.verticalHeader().setMinimumSectionSize(28)
        self.grid.setMaximumHeight(380)

        self.grid.cellChanged.connect(self._on_grid_cell_changed)
        self.grid.itemSelectionChanged.connect(self._on_grid_selection_changed)

        splitter.addWidget(self.grid)

        # Detail panel
        self.detail_panel = DetailPanel(self)
        self.detail_panel.parameter_changed.connect(self._on_parameter_changed)
        splitter.addWidget(self.detail_panel)

        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, True)
        splitter.setSizes([340, 400])

        layout.addWidget(splitter, stretch=1)

        # Status bar
        status_bar = QHBoxLayout()
        status_bar.setContentsMargins(0, 0, 0, 0)
        self.label_status = QLabel("Ready")
        self.label_status.setStyleSheet("color:#8899AA; font-size:11px;")
        status_bar.addWidget(self.label_status)
        status_bar.addStretch()
        layout.addLayout(status_bar)

        self.setLayout(layout)

    def _load_metrics(self):
        """Load strategy metrics from database."""
        try:
            self._metrics = self._metrics_calculator.compute_all_model_stats()
            logger.info(f"Loaded metrics for {len(self._metrics)} models")
        except Exception as e:
            logger.error(f"Error loading metrics: {e}")
            self._metrics = {}

    def _populate_grid(self):
        """Populate the strategy grid."""
        self.grid.blockSignals(True)
        self.grid.setRowCount(0)

        row = 0
        for model_def in STRATEGY_REGISTRY:
            # Apply filter
            if self._type_filter and model_def.model_type != self._type_filter:
                continue

            # Query current state
            enabled = is_model_enabled(model_def.name)
            weight = get_model_weight(model_def.name)
            stats = self._metrics.get(model_def.name, ModelStats(model_name=model_def.name))

            self.grid.insertRow(row)

            # Column 0: Enable checkbox
            checkbox = QCheckBox()
            checkbox.setChecked(enabled)
            checkbox.stateChanged.connect(
                lambda state, m=model_def.name: self._on_model_enabled_toggled(m, state)
            )
            self.grid.setCellWidget(row, 0, checkbox)

            # Column 1: Name
            item_name = _ci(model_def.display_name, align=Qt.AlignLeft)
            self.grid.setItem(row, 1, item_name)

            # Column 2: Type badge
            item_type = _ci(model_def.model_type, color=_COLOR_MAP[model_def.model_type][0])
            item_type.setFont(QFont("Courier", 10, QFont.Bold))
            self.grid.setItem(row, 2, item_type)

            # Column 3: Weight spinbox
            spinbox = QDoubleSpinBox()
            spinbox.setMinimum(0.0)
            spinbox.setMaximum(1.0)
            spinbox.setSingleStep(0.01)
            spinbox.setDecimals(2)
            spinbox.setValue(weight)
            spinbox.setEnabled(enabled)
            spinbox.setAlignment(Qt.AlignCenter)
            spinbox.setStyleSheet(
                "QDoubleSpinBox { background:#0A0E1A; color:#E8EBF0; "
                "border:none; padding:2px 8px; font-size:13px; }"
                "QDoubleSpinBox::up-button, QDoubleSpinBox::down-button { width:0; }"
            )
            spinbox.valueChanged.connect(
                lambda val, m=model_def.name, k=model_def.weight_key:
                    self._on_weight_changed(m, k, val)
            )
            self.grid.setCellWidget(row, 3, spinbox)

            # Column 4: Status
            if not enabled:
                status_text = "Disabled"
                status_color = "#888888"
            elif weight == 0.0 and model_def.model_type == "ML":
                status_text = "Warming"
                status_color = "#CC8800"
            else:
                status_text = "Active"
                status_color = "#2E8B57"

            item_status = _ci(status_text, color=status_color)
            item_status.setFont(QFont("Courier", 10, QFont.Bold))
            self.grid.setItem(row, 4, item_status)

            # Column 5-9: Metrics
            wr_text = _fmt_pct(stats.win_rate)
            wr_color = "#2E8B57" if stats.win_rate > 0.50 else ("#FF4455" if stats.win_rate < 0.45 else "#E8EBF0")
            item_wr = _ci(wr_text, color=wr_color)
            self.grid.setItem(row, 5, item_wr)

            pf_text = _fmt_pct(stats.profit_factor, decimals=2) if stats.profit_factor != float('inf') else "∞"
            pf_color = "#2E8B57" if stats.profit_factor > 1.2 else ("#FF4455" if (stats.profit_factor < 1.0 and stats.profit_factor > 0) else "#E8EBF0")
            item_pf = _ci(pf_text, color=pf_color)
            self.grid.setItem(row, 6, item_pf)

            item_trades = _ci(str(stats.trade_count))
            self.grid.setItem(row, 7, item_trades)

            pnl_text = _fmt_currency(stats.total_pnl_usdt)
            pnl_color = "#2E8B57" if stats.total_pnl_usdt > 0 else ("#FF4455" if stats.total_pnl_usdt < 0 else "#E8EBF0")
            item_pnl = _ci(pnl_text, color=pnl_color)
            self.grid.setItem(row, 8, item_pnl)

            last_signal_text = _fmt_time_ago(stats.last_signal_ts)
            item_last = _ci(last_signal_text, align=Qt.AlignLeft)
            self.grid.setItem(row, 9, item_last)

            # Row styling for disabled models
            if not enabled:
                for col in range(10):
                    if col not in (1, 2, 4):
                        item = self.grid.item(row, col)
                        if item:
                            item.setBackground(QColor("#0A0A0A"))

            row += 1

        self.grid.blockSignals(False)

    def _refresh_metrics(self):
        """Refresh metrics in the grid (timer callback)."""
        try:
            self._load_metrics()

            # Rebuild grid with new metrics but preserve UI state
            row = 0
            for model_def in STRATEGY_REGISTRY:
                if self._type_filter and model_def.model_type != self._type_filter:
                    continue

                stats = self._metrics.get(model_def.name, ModelStats(model_name=model_def.name))

                # Update metrics columns (5-9)
                wr_text = _fmt_pct(stats.win_rate)
                wr_color = "#2E8B57" if stats.win_rate > 0.50 else ("#FF4455" if stats.win_rate < 0.45 else "#E8EBF0")
                self.grid.item(row, 5).setText(wr_text)
                self.grid.item(row, 5).setForeground(QColor(wr_color))

                pf_text = _fmt_pct(stats.profit_factor, decimals=2) if stats.profit_factor != float('inf') else "∞"
                pf_color = "#2E8B57" if stats.profit_factor > 1.2 else ("#FF4455" if (stats.profit_factor < 1.0 and stats.profit_factor > 0) else "#E8EBF0")
                self.grid.item(row, 6).setText(pf_text)
                self.grid.item(row, 6).setForeground(QColor(pf_color))

                self.grid.item(row, 7).setText(str(stats.trade_count))

                pnl_text = _fmt_currency(stats.total_pnl_usdt)
                pnl_color = "#2E8B57" if stats.total_pnl_usdt > 0 else ("#FF4455" if stats.total_pnl_usdt < 0 else "#E8EBF0")
                self.grid.item(row, 8).setText(pnl_text)
                self.grid.item(row, 8).setForeground(QColor(pnl_color))

                last_signal_text = _fmt_time_ago(stats.last_signal_ts)
                self.grid.item(row, 9).setText(last_signal_text)

                row += 1
        except Exception as e:
            logger.error(f"Error refreshing metrics: {e}")

    def _start_metrics_timer(self):
        """Start 60s timer to refresh metrics."""
        self.metrics_timer = QTimer()
        self.metrics_timer.timeout.connect(self._refresh_metrics)
        self.metrics_timer.start(60000)  # 60s

    def _apply_type_filter(self, model_type: Optional[str]):
        """Apply type filter to grid."""
        self._type_filter = model_type

        # Update button states
        self.btn_filter_all.setChecked(model_type is None)
        for mtype, btn in self.filter_buttons.items():
            btn.setChecked(model_type == mtype)

        # Repopulate grid
        self._populate_grid()

    @Slot(int)
    def _on_model_enabled_toggled(self, model_name: str, state: int):
        """Handle enable/disable checkbox."""
        enabled = state == Qt.Checked

        # Find row
        row = None
        visible_row = 0
        for r, model_def in enumerate(STRATEGY_REGISTRY):
            if self._type_filter and model_def.model_type != self._type_filter:
                continue
            if model_def.name == model_name:
                row = visible_row
                break
            visible_row += 1

        if row is None:
            return

        # Update weight spinbox
        spinbox = self.grid.cellWidget(row, 3)
        if spinbox:
            spinbox.setEnabled(enabled)

        # Mark dirty
        model_def = get_model_def(model_name)
        if model_def:
            old_enabled = is_model_enabled(model_name)
            if old_enabled != enabled:
                # Mark the disabled_models list as dirty
                disabled = settings.get("disabled_models", [])
                if enabled:
                    new_disabled = [m for m in disabled if m != model_name]
                else:
                    new_disabled = list(set(disabled + [model_name]))

                self._dirty["disabled_models"] = (disabled, new_disabled)
                self._update_dirty_indicator()

    @Slot(float)
    def _on_weight_changed(self, model_name: str, weight_key: str, new_weight: float):
        """Handle weight spinbox change."""
        old_weight = get_model_weight(model_name)
        if abs(old_weight - new_weight) > 0.001:  # Avoid float comparison issues
            self._dirty[weight_key] = (old_weight, new_weight)
            self._update_dirty_indicator()

    @Slot(str, object)
    def _on_parameter_changed(self, param_key: str, new_value: Any):
        """Handle parameter change from detail panel."""
        old_value = settings.get(param_key)
        if old_value != new_value:
            self._dirty[param_key] = (old_value, new_value)
            self._update_dirty_indicator()

    def _update_dirty_indicator(self):
        """Update UI to show dirty status."""
        if self._dirty:
            count = len(self._dirty)
            self.label_dirty.setText(f"◉ {count} unsaved change{'s' if count > 1 else ''}")
            self.btn_save.setEnabled(True)
        else:
            self.label_dirty.setText("")
            self.btn_save.setEnabled(False)

    @Slot()
    def _on_grid_selection_changed(self):
        """Handle grid row selection."""
        selected = self.grid.selectionModel().selectedRows()
        if selected:
            row = selected[0].row()
            # Find the actual model (accounting for filter)
            visible_row = 0
            for model_def in STRATEGY_REGISTRY:
                if self._type_filter and model_def.model_type != self._type_filter:
                    continue
                if visible_row == row:
                    self.detail_panel.load_model(model_def)
                    break
                visible_row += 1

    def _on_grid_cell_changed(self, row: int, col: int):
        """Handle grid cell edits."""
        # Ignored — weight changes handled by spinbox
        pass

    def _validate_changes(self) -> Tuple[bool, List[str]]:
        """
        Validate all pending changes.

        Returns
        -------
        (is_valid, error_messages)
        """
        errors = []

        # At least 1 core model must be enabled
        disabled_old, disabled_new = self._dirty.get("disabled_models", (settings.get("disabled_models", []), settings.get("disabled_models", [])))
        disabled = disabled_new if disabled_new is not None else settings.get("disabled_models", [])

        core_models = [m.name for m in STRATEGY_REGISTRY if m.model_type == "CORE"]
        enabled_core = [m for m in core_models if m not in disabled]

        if not enabled_core:
            errors.append("At least one CORE model must be enabled.")

        # Weight validations
        for key, (old_val, new_val) in self._dirty.items():
            if key.startswith("model_weights."):
                if not (0.0 <= new_val <= 1.0):
                    errors.append(f"Weight {key}: must be between 0.0 and 1.0, got {new_val}")

        # Confluence threshold validation
        if "idss.min_confluence_score" in self._dirty:
            _, new_val = self._dirty["idss.min_confluence_score"]
            if not (0.2 <= new_val <= 0.9):
                errors.append(f"Confluence threshold must be between 0.2 and 0.9")

        # Regime affinity validation
        for key, (old_val, new_val) in self._dirty.items():
            if key.startswith("regime_affinity."):
                if not (0.0 <= new_val <= 1.0):
                    errors.append(f"Regime affinity {key}: must be between 0.0 and 1.0, got {new_val}")

        return len(errors) == 0, errors

    @Slot()
    def _on_save_all(self):
        """Save all pending changes."""
        is_valid, errors = self._validate_changes()

        if not is_valid:
            QMessageBox.critical(
                self, "Validation Failed",
                "Cannot save changes:\n\n" + "\n".join(errors)
            )
            return

        # Show confirmation
        count = len(self._dirty)
        reply = QMessageBox.question(
            self, "Confirm Save",
            f"Save {count} change{'s' if count > 1 else ''}?",
            QMessageBox.Yes | QMessageBox.No
        )

        if reply != QMessageBox.Yes:
            return

        # Apply changes
        try:
            for key, (old_val, new_val) in self._dirty.items():
                settings.set(key, new_val, auto_save=False)

                # Log change
                model_name = "system"
                if key.startswith("model_weights."):
                    for m in STRATEGY_REGISTRY:
                        if m.weight_key == key:
                            model_name = m.name
                            break
                elif key.startswith("models."):
                    # Extract model from key like "models.trend.adx_min"
                    parts = key.split(".")
                    if len(parts) >= 2:
                        model_name = parts[1]

                self._audit_logger.log_change(
                    action="param_change",
                    model=model_name,
                    key=key,
                    old_value=old_val,
                    new_value=new_val
                )

            # Save config
            settings.save()

            # Clear dirty state
            self._dirty.clear()
            self._update_dirty_indicator()

            # Refresh grid
            self._load_metrics()
            self._populate_grid()

            self.label_status.setText(f"✓ Saved {count} change(s) at {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}")

            QMessageBox.information(
                self, "Success",
                f"Saved {count} change(s)."
            )
        except Exception as e:
            logger.error(f"Error saving changes: {e}")
            QMessageBox.critical(
                self, "Save Error",
                f"Failed to save changes:\n\n{e}"
            )

    @Slot()
    def _on_restore_all(self):
        """Restore all parameters to defaults."""
        reply = QMessageBox.question(
            self, "Restore Defaults",
            "Reset ALL parameters to factory defaults?\n\n"
            "This cannot be undone without re-saving your current settings.",
            QMessageBox.Yes | QMessageBox.No
        )

        if reply != QMessageBox.Yes:
            return

        # Collect all defaults
        defaults = {}
        for model_def in STRATEGY_REGISTRY:
            defaults[model_def.weight_key] = model_def.default_weight
            for param in model_def.params:
                defaults[param.key] = param.default

        for param in GLOBAL_PARAMS:
            defaults[param.key] = param.default

        defaults["disabled_models"] = []

        # Restore regime affinity defaults
        from core.meta_decision.confluence_scorer import REGIME_AFFINITY
        for model_name, regimes in REGIME_AFFINITY.items():
            for regime, val in regimes.items():
                key = f"regime_affinity.{model_name}.{regime}"
                current = settings.get(key, val)
                if current != val:
                    defaults[key] = val

        # Apply to dirty dict (don't save yet)
        self._dirty.clear()
        for key, default_val in defaults.items():
            current_val = settings.get(key)
            if current_val != default_val:
                self._dirty[key] = (current_val, default_val)

        self._update_dirty_indicator()

        QMessageBox.information(
            self, "Defaults Loaded",
            f"Loaded {len(self._dirty)} default values.\n\n"
            "Click 'Save All Changes' to persist."
        )
