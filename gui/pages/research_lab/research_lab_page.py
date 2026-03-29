"""
gui/pages/research_lab/research_lab_page.py
=============================================
Research Lab — first-class NexusTrader module.

Architecture
------------
  ResearchLabPage          — top-level QWidget registered in main_window
    ├─ _LabState           — isolated state container (no shared singleton)
    ├─ _BaselinePanel      — run canonical baseline, show PASS/FAIL
    ├─ _ParameterPanel     — per-param FIXED/OPTIMIZE toggle + range
    ├─ _SearchPanel        — sweep strategy + run controls
    ├─ _ProgressPanel      — live trial counter, best PF, leaderboard preview
    ├─ _ResultsPanel       — full results table with sort/filter
    └─ _ValidationPanel    — IS/OOS split metrics

All heavy work runs in SweepWorkerThread (QThread) → communicates via Qt signals.
The UI never blocks; all data flows through queued signal connections.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import (
    QDate, QObject, QThread, Qt, QTimer, Signal, Slot,
)
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDateEdit, QFrame, QGridLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QProgressBar, QPushButton,
    QScrollArea, QSizePolicy, QSlider, QSpinBox, QSplitter,
    QStackedWidget, QTableWidget, QTableWidgetItem, QTextEdit,
    QVBoxLayout, QWidget, QDoubleSpinBox, QCheckBox, QLineEdit,
)

ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(ROOT))

logger = logging.getLogger(__name__)

# Persists the last baseline run result across restarts
_BASELINE_CACHE = ROOT / "research" / "engine" / "last_baseline_result.json"

# ── Colour palette ─────────────────────────────────────────────────────────
_GREEN  = "#4caf50"
_RED    = "#f44336"
_AMBER  = "#ff9800"
_BLUE   = "#2196f3"
_DARK   = "#1a1a2e"
_PANEL  = "#16213e"
_CARD   = "#0f3460"
_TEXT   = "#e0e0e0"
_DIM    = "#888888"
_ACCENT = "#e94560"


def _label(text: str, bold: bool = False, color: str = _TEXT, size: int = 0) -> QLabel:
    lbl = QLabel(text)
    f = lbl.font()
    if bold:
        f.setBold(True)
    if size:
        f.setPointSize(size)
    lbl.setFont(f)
    lbl.setStyleSheet(f"color: {color};")
    return lbl


def _card(title: str) -> tuple[QGroupBox, QVBoxLayout]:
    gb = QGroupBox(title)
    gb.setStyleSheet(f"""
        QGroupBox {{
            background: {_CARD};
            border: 1px solid #1e3050;
            border-radius: 3px;
            margin-top: 8px;
            padding: 4px;
            color: {_TEXT};
            font-weight: bold;
        }}
        QGroupBox::title {{
            subcontrol-origin: margin;
            left: 10px;
            padding: 0 4px;
        }}
    """)
    lay = QVBoxLayout(gb)
    lay.setSpacing(4)
    lay.setContentsMargins(4, 10, 4, 4)
    return gb, lay


def _hsep() -> "QFrame":
    """Thin 1 px horizontal separator — matches the Data Status table grid lines."""
    sep = QFrame()
    sep.setFrameShape(QFrame.HLine)
    sep.setFrameShadow(QFrame.Plain)
    sep.setFixedHeight(1)
    sep.setStyleSheet("background:#2d2d2d; border:none;")
    return sep


# ─────────────────────────────────────────────────────────────────────────────
# Isolated state object
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _LabState:
    """All mutable state for the Research Lab. No external singletons."""
    baseline_status:    str  = "NOT RUN"  # "NOT RUN" | "PASS" | "FAIL" | "RUNNING"
    baseline_metrics:   dict = field(default_factory=dict)
    sweep_running:      bool = False
    trials_completed:   int  = 0
    trials_total:       int  = 0
    best_pf:            float= 0.0
    current_experiment: str  = ""
    leaderboard:        list = field(default_factory=list)
    # param mode: settings_key → "FIXED" | "OPTIMIZE"
    param_modes:        dict = field(default_factory=dict)
    # param overrides: settings_key → custom_value (for FIXED overrides)
    param_values:       dict = field(default_factory=dict)
    search_strategy:    str  = "coarse"      # "coarse"|"random"|"bayesian"
    n_random_trials:    int  = 100
    n_workers:          int  = field(default_factory=lambda: max(2, __import__('multiprocessing').cpu_count() - 2))
    objective:          str  = "profit_factor"
    cost_per_side:      float= 0.0004
    # Phase 1 additions: period + asset selection
    date_start:         str  = "2022-03-22"
    date_end:           str  = "2026-03-21"
    selected_symbols:   list = field(default_factory=lambda: ["BTC/USDT", "SOL/USDT", "ETH/USDT"])
    # Phase 2 additions: unified engine mode
    backtest_mode:      str  = "pbl_slc"    # BacktestRunner.MODE_* constant
    strategy_subset:    list = field(default_factory=list)  # non-empty only for "custom" mode
    # Session 41: technical-only confluence mode
    confluence_mode:    str  = "none"       # "none" | "technical_only"
    # Session 42: orchestration mode for full_system unified engine
    orchestration_mode: str  = "naive"      # "naive" | "research_priority"
    # Session 43: HMM confidence gate threshold for TrendModel / MomentumBreakout
    hmm_confidence_min: float = 0.0         # 0.0 = no gating; 0.60/0.70/0.80 = gate active


# ─────────────────────────────────────────────────────────────────────────────
# Worker thread
# ─────────────────────────────────────────────────────────────────────────────

class SweepWorkerThread(QThread):
    """
    Runs baseline or sweep in background thread.
    All results → main thread via queued signals.
    """
    progress       = Signal(int, int, float, str)   # completed, total, best_pf, msg
    indeterminate  = Signal(bool)                   # True → pulsing bar, False → normal
    trial_done     = Signal(dict)                   # one trial result
    finished_ok    = Signal(dict)                   # final summary
    error_signal   = Signal(str)                    # error message
    cache_info_sig = Signal(dict)                   # cache HIT/MISS stats after load_data

    def __init__(
        self,
        mode:         str,   # "baseline" | "sweep"
        state:        _LabState,
        parent:       Optional[QObject] = None,
    ):
        super().__init__(parent)
        self._mode   = mode
        self._state  = state
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        """QThread.run() — executes in worker thread."""
        try:
            if self._mode == "baseline":
                self._run_baseline()
            else:
                self._run_sweep()
        except Exception as exc:
            logger.exception("SweepWorkerThread error")
            self.error_signal.emit(str(exc))

    def _run_baseline(self):
        from research.engine.backtest_runner import BacktestRunner
        from research.engine.baseline_registry import load_baseline

        PHASES = 4
        state  = self._state

        # Phase 1 — load data (indeterminate, long)
        self.indeterminate.emit(True)
        self.progress.emit(0, PHASES, 0.0, "Phase 1/4 — Loading historical data…")
        _mode     = getattr(state, "backtest_mode", "pbl_slc")
        _subset   = getattr(state, "strategy_subset", []) or None
        _orch     = getattr(state, "orchestration_mode", "naive")
        _conf_min = float(getattr(state, "hmm_confidence_min", 0.0))
        runner = BacktestRunner(
            date_start           = state.date_start,
            date_end             = state.date_end,
            symbols              = state.selected_symbols,
            mode                 = _mode,
            strategy_subset      = _subset,
            orchestration_mode   = _orch,
            hmm_confidence_min   = _conf_min,
        )
        # Wire granular load_data() progress into the UI progress signal.
        # progress_cb signature: (msg: str, pct: float)
        def _data_progress(msg: str, pct: float):
            self.progress.emit(int(pct), 100, 0.0, msg)

        runner.load_data(progress_cb=_data_progress)

        # Emit cache stats so the UI can show HIT/MISS summary
        self.cache_info_sig.emit(runner.cache_info())

        if self._cancel:
            return

        # Phase 2+3 — zero-fee and fee scenarios run in PARALLEL.
        # runner.run() is safe to call concurrently: all shared runner state
        # (_ind, _highs/_lows/_opens, _nx_regime, _master_ts) is read-only
        # after load_data() completes.  Each run() creates its own local
        # sig_gen, sizer, positions dict, and pending_entries, so there is no
        # mutable state shared between the two threads.
        # NumPy operations inside generate() release the GIL, giving true
        # CPU parallelism even under Python's threading model.
        import concurrent.futures as _cf
        self.progress.emit(1, PHASES, 0.0,
            "Phase 2+3/4 — Running zero-fee & fee scenarios in parallel…")
        with _cf.ThreadPoolExecutor(max_workers=2) as _ex:
            _f0 = _ex.submit(runner.run, {}, 0.0)
            _f1 = _ex.submit(runner.run, {}, 0.0004)
            r0 = _f0.result()
            r1 = _f1.result()
        if self._cancel:
            return

        # Phase 4 — validate (fast)
        self.indeterminate.emit(False)
        self.progress.emit(3, PHASES, 0.0, "Phase 4/4 — Validating against baseline lock…")
        bl = load_baseline()
        passed, failures = bl.check(r0, r1)
        summary = {
            "passed":   passed,
            "failures": failures,
            "r0":       {k: v for k, v in r0.items() if k != "all_trades"},
            "r1":       {k: v for k, v in r1.items() if k != "all_trades"},
        }
        self.progress.emit(PHASES, PHASES, r0.get("profit_factor", 0.0), "Done")
        self.finished_ok.emit(summary)

    def _run_sweep(self):
        from research.engine.parameter_registry import ALL_PARAMS
        from research.engine.sweep_engine import (
            SweepEngine, generate_coarse_grid, generate_random_trials,
        )
        from research.engine.experiment_store import ExperimentStore, TrialResult

        state = self._state
        param_defs = []
        for p in ALL_PARAMS:
            import copy
            pd = copy.copy(p)
            pd.mode = state.param_modes.get(p.settings_key, "FIXED")
            param_defs.append(pd)

        fixed = {k: v for k, v in state.param_values.items()}

        if state.search_strategy == "coarse":
            trials = generate_coarse_grid(param_defs, fixed)
        elif state.search_strategy == "random":
            trials = generate_random_trials(param_defs, fixed, state.n_random_trials)
        else:
            trials = generate_random_trials(param_defs, fixed, state.n_random_trials)

        total = len(trials)
        self.progress.emit(0, total, 0.0, f"Generated {total} trials")

        exp_id = state.current_experiment or ExperimentStore.new_id()
        store  = ExperimentStore(exp_id)
        _sw_mode     = getattr(state, "backtest_mode", "pbl_slc")
        _sw_subset   = getattr(state, "strategy_subset", []) or None
        _sw_conf     = getattr(state, "confluence_mode", "none")
        _sw_orch     = getattr(state, "orchestration_mode", "naive")
        _sw_conf_min = float(getattr(state, "hmm_confidence_min", 0.0))
        engine = SweepEngine(
            n_workers            = state.n_workers,
            date_start           = state.date_start,
            date_end             = state.date_end,
            symbols              = state.selected_symbols or None,
            mode                 = _sw_mode,
            strategy_subset      = _sw_subset,
            confluence_mode      = _sw_conf,
            orchestration_mode   = _sw_orch,
            hmm_confidence_min   = _sw_conf_min,
        )
        best_pf = 0.0

        for result in engine.run_sweep(trials, state.cost_per_side):
            if self._cancel:
                break
            completed = result.get("trial_id", 0) + 1
            pf = result.get("profit_factor", 0.0)
            if pf > best_pf:
                best_pf = pf

            tr = TrialResult(
                trial_id      = result.get("trial_id", 0),
                params        = result.get("params_applied", {}),
                n_trades      = result.get("n_trades", 0),
                profit_factor = pf,
                win_rate      = result.get("win_rate", 0.0),
                cagr          = result.get("cagr", 0.0),
                max_drawdown  = result.get("max_drawdown", 0.0),
                pbl_n         = result.get("pbl_n", 0),
                pbl_pf        = result.get("pbl_pf", 0.0),
                slc_n         = result.get("slc_n", 0),
                slc_pf        = result.get("slc_pf", 0.0),
                elapsed_s     = result.get("elapsed_s", 0.0),
                status        = result.get("status", "ok"),
                error         = result.get("error", ""),
            )
            store.append_trial(tr)
            self.trial_done.emit(result)
            self.progress.emit(completed, total, best_pf,
                               f"Trial {completed}/{total} — best PF {best_pf:.4f}")

        summary = {"experiment_id": exp_id, "total": total, "best_pf": best_pf}
        self.finished_ok.emit(summary)


# ─────────────────────────────────────────────────────────────────────────────
# Baseline Panel
# ─────────────────────────────────────────────────────────────────────────────

class _BaselinePanel(QWidget):
    baseline_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build()

    def _build(self):
        card, vlay = _card("◈  Baseline")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)

        self._status_lbl = _label("NOT RUN", bold=True, color=_TEXT, size=13)
        vlay.addWidget(self._status_lbl)

        self._metrics_text = QTextEdit()
        self._metrics_text.setReadOnly(True)
        self._metrics_text.setMaximumHeight(120)
        self._metrics_text.setStyleSheet(
            f"background:{_DARK}; color:{_TEXT}; border:none; font-size:13px;"
        )
        self._metrics_text.setPlainText("Run baseline to validate canonical engine.")
        vlay.addWidget(self._metrics_text)

        self._btn = QPushButton("▶  Run Baseline")
        self._btn.setStyleSheet(
            f"background:{_BLUE}; color:white; font-weight:bold; padding:6px;"
            " border-radius:4px;"
        )
        self._btn.clicked.connect(self.baseline_requested)
        vlay.addWidget(self._btn)

    def set_running(self):
        self._status_lbl.setText("RUNNING…")
        self._status_lbl.setStyleSheet(f"color:{_AMBER}; font-weight:bold;")
        self._btn.setEnabled(False)

    def set_result(self, summary: dict, saved_at: str = ""):
        self._btn.setEnabled(True)
        passed = summary.get("passed", False)
        r0     = summary.get("r0", {})
        r1     = summary.get("r1", {})
        if passed:
            self._status_lbl.setText("PASS ✅")
            self._status_lbl.setStyleSheet(f"color:{_GREEN}; font-weight:bold;")
        else:
            self._status_lbl.setText("FAIL ❌")
            self._status_lbl.setStyleSheet(f"color:{_RED}; font-weight:bold;")

        failures = summary.get("failures", [])
        n_trades = r0.get('n_trades', 0)
        wr_raw   = r0.get('win_rate', 0)
        pf0      = r0.get('profit_factor', '?')
        pf_fees  = r1.get('profit_factor', '?')
        cagr     = r0.get('cagr', 0)
        maxdd    = r0.get('max_drawdown', 0)
        pbl_n    = r0.get('pbl_n', '?')
        pbl_pf   = r0.get('pbl_pf', '?')
        slc_n    = r0.get('slc_n', '?')
        slc_pf   = r0.get('slc_pf', '?')
        # Format win rate: stored as fraction (0.5638) → display as percentage (56.38%)
        wr_str = f"{wr_raw * 100:.2f}%" if isinstance(wr_raw, (int, float)) else str(wr_raw)
        lines = [
            f"Trades: {n_trades:,}   PF (no fees): {pf0}   PF (0.04%/side): {pf_fees}",
            f"Win Rate: {wr_str}   CAGR: {cagr:.1%}   Max DD: {maxdd:.1%}",
            f"PBL: {pbl_n:,} trades   PF: {pbl_pf}" if isinstance(pbl_n, int) else f"PBL: {pbl_n} trades   PF: {pbl_pf}",
            f"SLC: {slc_n:,} trades   PF: {slc_pf}" if isinstance(slc_n, int) else f"SLC: {slc_n} trades   PF: {slc_pf}",
        ]
        if saved_at:
            lines.append(f"(restored from {saved_at})")
        if failures:
            lines.append("FAILURES:")
            lines.extend(f"  • {f}" for f in failures)
        self._metrics_text.setPlainText("\n".join(lines))


# ─────────────────────────────────────────────────────────────────────────────
# Parameter Panel
# ─────────────────────────────────────────────────────────────────────────────

class _ParamRow(QWidget):
    """One row in the parameter panel: name | value | FIXED/OPTIMIZE toggle."""

    changed = Signal()

    def __init__(self, param_def, parent=None):
        super().__init__(parent)
        self._p = param_def
        self._build()

    def _build(self):
        # Give each row a dark background so no blue gap bleeds through
        self.setStyleSheet(f"background:{_DARK};")
        h = QHBoxLayout(self)
        h.setContentsMargins(8, 5, 8, 5)
        h.setSpacing(6)

        # Description — stretches to fill available space
        name = _label(self._p.description, color=_TEXT)
        name.setStyleSheet(f"color:{_TEXT}; background:transparent;")  # inherit row bg
        name.setMinimumWidth(130)
        name.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        h.addWidget(name, 1)

        # Value spinbox — wide enough to show "40.00" without clipping
        self._spin = QDoubleSpinBox()
        self._spin.setDecimals(2)
        self._spin.setRange(self._p.range_min * 0.5, self._p.range_max * 1.5)
        self._spin.setSingleStep(self._p.step)
        self._spin.setValue(float(self._p.default))
        self._spin.setFixedWidth(78)
        self._spin.setAlignment(Qt.AlignCenter)
        self._spin.setStyleSheet(
            f"background:{_DARK}; color:{_TEXT}; border:1px solid #444;"
            " border-radius:3px; padding: 0 4px;"
        )
        h.addWidget(self._spin)

        # Range label — wide enough for "[25.0–55.0]"
        range_lbl = _label(
            f"[{self._p.range_min}–{self._p.range_max}]",
            color=_DIM,
        )
        range_lbl.setFixedWidth(100)
        range_lbl.setAlignment(Qt.AlignCenter)
        range_lbl.setStyleSheet(f"color:{_DIM}; font-size:11px; background:transparent;")
        h.addWidget(range_lbl)

        # FIXED / OPTIMIZE toggle
        self._mode_btn = QPushButton("FIXED")
        self._mode_btn.setCheckable(True)
        self._mode_btn.setFixedWidth(86)
        self._mode_btn.setStyleSheet(self._btn_style(False))
        self._mode_btn.toggled.connect(self._on_toggle)
        h.addWidget(self._mode_btn)

    def _btn_style(self, optimize: bool) -> str:
        if optimize:
            return (
                f"background:{_ACCENT}; color:white; font-weight:bold;"
                " border-radius:3px; padding:4px;"
            )
        return (
            f"background:#333; color:{_TEXT}; font-weight:bold;"
            " border-radius:3px; padding:4px;"
        )

    def _on_toggle(self, checked: bool):
        self._mode_btn.setText("OPTIMIZE" if checked else "FIXED")
        self._mode_btn.setStyleSheet(self._btn_style(checked))
        self.changed.emit()

    @property
    def settings_key(self) -> str:
        return self._p.settings_key

    @property
    def mode(self) -> str:
        return "OPTIMIZE" if self._mode_btn.isChecked() else "FIXED"

    @property
    def value(self):
        return round(self._spin.value(), 6)


# ─────────────────────────────────────────────────────────────────────────────
# Strategy Selection Panel (Phase 2 — unified engine)
# ─────────────────────────────────────────────────────────────────────────────

_STRATEGY_OPTIONS: list[tuple[str, str, str]] = [
    # (mode_key, display_label, description)
    ("pbl_slc",     "PBL + SLC",       "Pullback Long + Swing Low Continuation  [research regime]"),
    ("pbl",         "PBL only",        "PullbackLong only  [research bull_trend regime]"),
    ("slc",         "SLC only",        "SwingLowContinuation only  [research bear_trend regime]"),
    ("trend",       "Trend Model",     "TrendModel — directional trend following  [HMM regime]"),
    ("momentum",    "Momentum",        "MomentumBreakout — vol-expansion breakouts  [HMM regime]"),
    ("full_system", "Full System",     "All strategies: PBL + SLC + Trend + Momentum"),
    ("custom",      "Custom ▾",        "Choose individual models below"),
]

_CUSTOM_MODELS: list[tuple[str, str]] = [
    ("pullback_long",          "PBL  (Pullback Long)"),
    ("swing_low_continuation", "SLC  (Swing Low Continuation)"),
    ("trend",                  "Trend Model"),
    ("momentum_breakout",      "Momentum Breakout"),
]


class _StrategyPanel(QWidget):
    """
    Radio-button strategy selector feeding the unified BacktestRunner.

    Signals
    -------
    mode_changed(mode: str, subset: list[str])
        Emitted whenever the user changes mode or custom checkboxes.
        - mode   : BacktestRunner.MODE_* constant string
        - subset : non-empty list only when mode=="custom", else []
    """

    mode_changed = Signal(str, list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._radios:         dict[str, "QRadioButton"] = {}   # mode_key → radio
        self._custom_checks:  dict[str, QCheckBox]      = {}   # model_name → checkbox
        self._custom_box:     Optional[QWidget]          = None
        self._build()

    # ── Build ────────────────────────────────────────────────────────────────

    def _build(self):
        # Import here to avoid circular at module parse time
        from PySide6.QtWidgets import QRadioButton, QButtonGroup

        card, vlay = _card("🎯  Strategy")
        vlay.setSpacing(0)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)

        self._btn_group = QButtonGroup(self)
        self._btn_group.setExclusive(True)

        for mode_key, label, desc in _STRATEGY_OPTIONS:
            row_w = QWidget()
            row_w.setStyleSheet(f"background:{_DARK};")
            row_h = QHBoxLayout(row_w)
            row_h.setContentsMargins(8, 5, 8, 5)
            row_h.setSpacing(8)

            rb = QRadioButton(label)
            rb.setStyleSheet(
                f"QRadioButton {{ color:{_TEXT}; font-size:13px; background:transparent; }}"
                f" QRadioButton::indicator {{ width:13px; height:13px; }}"
            )
            rb.setChecked(mode_key == "pbl_slc")
            rb.toggled.connect(lambda checked, k=mode_key: self._on_radio(k, checked))
            self._radios[mode_key] = rb
            self._btn_group.addButton(rb)
            row_h.addWidget(rb)

            desc_lbl = _label(desc, color=_DIM)
            desc_lbl.setStyleSheet(f"color:{_DIM}; font-size:11px; background:transparent;")
            row_h.addWidget(desc_lbl, 1)

            vlay.addWidget(row_w)
            vlay.addWidget(_hsep())

        # Custom model checkboxes (initially hidden)
        self._custom_box = QWidget()
        self._custom_box.setStyleSheet(f"background:{_PANEL}; border-left:3px solid {_BLUE};")
        cbox_v = QVBoxLayout(self._custom_box)
        cbox_v.setContentsMargins(14, 6, 8, 6)
        cbox_v.setSpacing(4)

        for model_name, model_label in _CUSTOM_MODELS:
            cb = QCheckBox(model_label)
            cb.setChecked(model_name in ("pullback_long", "swing_low_continuation"))
            cb.setStyleSheet(
                f"QCheckBox {{ color:{_TEXT}; font-size:12px; background:transparent; }}"
            )
            cb.toggled.connect(self._on_custom_changed)
            self._custom_checks[model_name] = cb
            cbox_v.addWidget(cb)

        self._custom_box.setVisible(False)
        vlay.addWidget(self._custom_box)

    # ── Slots ────────────────────────────────────────────────────────────────

    def _on_radio(self, mode_key: str, checked: bool):
        if not checked:
            return
        is_custom = (mode_key == "custom")
        if self._custom_box is not None:
            self._custom_box.setVisible(is_custom)
        subset = self._custom_subset() if is_custom else []
        self.mode_changed.emit(mode_key, subset)

    def _on_custom_changed(self):
        self.mode_changed.emit("custom", self._custom_subset())

    def _custom_subset(self) -> list[str]:
        return [m for m, cb in self._custom_checks.items() if cb.isChecked()]

    # ── Public API ───────────────────────────────────────────────────────────

    def current_mode(self) -> str:
        for mode_key, rb in self._radios.items():
            if rb.isChecked():
                return mode_key
        return "pbl_slc"

    def current_subset(self) -> list[str]:
        if self.current_mode() == "custom":
            return self._custom_subset()
        return []


class _ParameterPanel(QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[_ParamRow] = []
        self._card_widget: Optional[QWidget] = None
        self._build_for_mode("pbl_slc")

    def rebuild(self, mode: str) -> None:
        """Rebuild parameter rows for the given engine mode."""
        # Remove old card
        old = self.layout()
        if old:
            while old.count():
                item = old.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
        self._rows = []
        self._build_for_mode(mode)

    def _build_for_mode(self, mode: str):
        from research.engine.parameter_registry import params_for_mode

        params = params_for_mode(mode)

        card, vlay = _card("⊙  Parameters")
        vlay.setSpacing(0)

        if self.layout() is None:
            outer = QVBoxLayout(self)
            outer.setContentsMargins(0, 0, 0, 0)
        else:
            outer = self.layout()

        outer.addWidget(card)

        # ── Section colours ───────────────────────────────────────────────
        _SEC_COLORS = {
            "pbl":      (_BLUE,   "PBL — Pullback Long"),
            "slc":      (_AMBER,  "SLC — Swing Low Continuation"),
            "trend":    ("#7ecbff","Trend Model"),
            "momentum": ("#c8a0f0","Momentum Breakout"),
        }

        # Group params by model family, preserve original order
        seen_models: list[str] = []
        for p in params:
            if p.model not in seen_models:
                seen_models.append(p.model)

        for model_key in seen_models:
            color, title = _SEC_COLORS.get(model_key, (_TEXT, model_key.upper()))
            # Full-width header widget so background fills the entire row
            hdr_w = QWidget()
            hdr_w.setStyleSheet(f"background:{_PANEL};")
            hdr_lay = QHBoxLayout(hdr_w)
            hdr_lay.setContentsMargins(8, 4, 8, 4)
            hdr_lay.setSpacing(0)
            hdr_lbl = _label(title, bold=True, color=color)
            hdr_lbl.setStyleSheet(
                f"color:{color}; font-weight:bold; background:transparent; font-size:12px;"
            )
            hdr_lay.addWidget(hdr_lbl)
            hdr_lay.addStretch()
            vlay.addWidget(hdr_w)
            vlay.addWidget(_hsep())

            for p in [pp for pp in params if pp.model == model_key]:
                row = _ParamRow(p)
                self._rows.append(row)
                vlay.addWidget(row)
                vlay.addWidget(_hsep())

        vlay.addSpacing(4)

        reset_btn = QPushButton("Reset to Defaults")
        reset_btn.setStyleSheet(
            f"background:#333; color:{_TEXT}; border-radius:3px; padding:4px; font-size:13px;"
        )
        reset_btn.clicked.connect(self._reset_defaults)
        vlay.addWidget(reset_btn)

    def _reset_defaults(self):
        for row in self._rows:
            row._spin.setValue(float(row._p.default))
            row._mode_btn.setChecked(False)

    def get_state(self) -> tuple[dict, dict]:
        """Return (param_modes, param_values) for LabState."""
        modes  = {}
        values = {}
        for row in self._rows:
            modes[row.settings_key]  = row.mode
            values[row.settings_key] = row.value
        return modes, values

    def has_optimize(self) -> bool:
        return any(r.mode == "OPTIMIZE" for r in self._rows)


# ─────────────────────────────────────────────────────────────────────────────
# Search Panel
# ─────────────────────────────────────────────────────────────────────────────

class _SearchPanel(QWidget):
    start_requested  = Signal()
    cancel_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build()

    def _build(self):
        card, vlay = _card("⊟  Search")
        vlay.setSpacing(0)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)

        _cb_style = (
            f"QComboBox {{ background:{_DARK}; color:{_TEXT}; border:1px solid #2d2d2d;"
            f" padding:2px 4px; font-size:12px; }}"
            f" QComboBox::drop-down {{ border:none; }}"
            f" QComboBox QAbstractItemView {{ background:{_DARK}; color:{_TEXT};"
            f" selection-background-color:#1a3555; border:1px solid #2d2d2d; }}"
        )
        _sp_style = (
            f"QSpinBox {{ background:{_DARK}; color:{_TEXT}; border:1px solid #2d2d2d;"
            f" padding:2px 4px; font-size:12px; }}"
        )

        def _form_row(label_text: str, widget: QWidget) -> QWidget:
            """Wrap label+widget in a dark-background row widget."""
            row_w = QWidget()
            row_w.setStyleSheet(f"background:{_DARK};")
            row_h = QHBoxLayout(row_w)
            row_h.setContentsMargins(8, 4, 8, 4)
            row_h.setSpacing(6)
            lbl = _label(label_text, color=_TEXT)
            lbl.setStyleSheet(f"color:{_TEXT}; background:transparent; font-size:12px;")
            lbl.setFixedWidth(70)
            row_h.addWidget(lbl)
            row_h.addWidget(widget, 1)
            return row_w

        # Strategy
        self._strategy_cb = QComboBox()
        self._strategy_cb.addItems(["Coarse Grid Sweep", "Random Search", "Bayesian (coming soon)"])
        self._strategy_cb.setStyleSheet(_cb_style)
        vlay.addWidget(_form_row("Strategy:", self._strategy_cb))
        vlay.addWidget(_hsep())

        # Random trials
        self._n_trials_spin = QSpinBox()
        self._n_trials_spin.setRange(10, 10000)
        self._n_trials_spin.setValue(200)
        self._n_trials_spin.setStyleSheet(_sp_style)
        vlay.addWidget(_form_row("Trials:", self._n_trials_spin))
        vlay.addWidget(_hsep())

        # Workers — scale to actual CPU count so the machine is fully utilised.
        # Leave 2 logical cores headroom for OS + NexusTrader live scanner.
        import multiprocessing as _mp
        _cpu = _mp.cpu_count()
        _default_workers = max(2, _cpu - 2)
        self._workers_spin = QSpinBox()
        self._workers_spin.setRange(1, _cpu)
        self._workers_spin.setValue(_default_workers)
        self._workers_spin.setStyleSheet(_sp_style)
        vlay.addWidget(_form_row(f"Workers (max {_cpu}):", self._workers_spin))
        vlay.addWidget(_hsep())

        # Objective
        self._obj_cb = QComboBox()
        self._obj_cb.addItems(["Profit Factor", "CAGR", "Win Rate"])
        self._obj_cb.setStyleSheet(_cb_style)
        vlay.addWidget(_form_row("Objective:", self._obj_cb))
        vlay.addWidget(_hsep())

        # Fee model
        self._fee_cb = QComboBox()
        self._fee_cb.addItems(["0.04%/side (production)", "Zero fees"])
        self._fee_cb.setStyleSheet(_cb_style)
        vlay.addWidget(_form_row("Fees:", self._fee_cb))
        vlay.addWidget(_hsep())

        # Confluence mode (Session 41)
        self._conf_cb = QComboBox()
        self._conf_cb.addItems(["None (highest-strength)", "Technical Only"])
        self._conf_cb.setStyleSheet(_cb_style)
        self._conf_cb.setToolTip(
            "None: highest-strength single-winner (default)\n"
            "Technical Only: ConfluenceScorer gate — model weights, regime\n"
            "affinity, direction dominance, correlation dampening, dynamic\n"
            "threshold.  Excludes: Orchestrator, L1/L2, OI/Liq modifiers."
        )
        vlay.addWidget(_form_row("Confluence:", self._conf_cb))
        vlay.addWidget(_hsep())

        # Orchestration mode (Session 42)
        self._orch_cb = QComboBox()
        self._orch_cb.addItems(["Naive (all compete)", "Research Priority"])
        self._orch_cb.setStyleSheet(_cb_style)
        self._orch_cb.setToolTip(
            "Naive: all models compete; highest-strength signal wins (original).\n"
            "Research Priority: PBL + SLC take priority over Trend + Momentum\n"
            "when multiple models fire on the same bar/symbol.  HMM models\n"
            "only fire when no research signal exists for that bar.\n"
            "Only affects full_system and custom modes; pbl_slc is unaffected."
        )
        vlay.addWidget(_form_row("Orchestration:", self._orch_cb))
        vlay.addWidget(_hsep())

        # HMM Confidence Gate (Session 43)
        self._hmm_conf_cb = QComboBox()
        self._hmm_conf_cb.addItems([
            "Off (no gate)",
            "0.60 — moderate",
            "0.70 — recommended",
            "0.80 — strict",
        ])
        self._hmm_conf_cb.setStyleSheet(_cb_style)
        self._hmm_conf_cb.setToolTip(
            "Minimum HMM posterior confidence for TrendModel and MomentumBreakout\n"
            "to generate signals.  When the HMM regime classifier confidence is\n"
            "below the threshold, those models are silenced for that bar.\n"
            "Off = original behavior (no gating).\n"
            "0.70 = recommended starting point based on Session 43 validation.\n"
            "Only affects full_system and custom modes; pbl_slc is unaffected."
        )
        vlay.addWidget(_form_row("HMM Conf Gate:", self._hmm_conf_cb))
        vlay.addWidget(_hsep())

        vlay.addSpacing(6)

        # Start / Cancel buttons
        btn_row = QHBoxLayout()
        self._start_btn = QPushButton("▶  Start Sweep")
        self._start_btn.setStyleSheet(
            f"background:{_GREEN}; color:white; font-weight:bold;"
            " padding:7px; border-radius:4px;"
        )
        self._start_btn.clicked.connect(self.start_requested)
        btn_row.addWidget(self._start_btn)

        self._cancel_btn = QPushButton("■  Cancel")
        self._cancel_btn.setStyleSheet(
            f"background:{_RED}; color:white; font-weight:bold;"
            " padding:7px; border-radius:4px;"
        )
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.clicked.connect(self.cancel_requested)
        btn_row.addWidget(self._cancel_btn)
        vlay.addLayout(btn_row)

    def get_state(self) -> dict:
        strategy_map = {
            0: "coarse",
            1: "random",
            2: "bayesian",
        }
        fee_map      = {0: 0.0004, 1: 0.0}
        conf_map     = {0: "none", 1: "technical_only"}
        orch_map     = {0: "naive", 1: "research_priority"}
        hmm_conf_map = {0: 0.0, 1: 0.60, 2: 0.70, 3: 0.80}
        return {
            "search_strategy":    strategy_map[self._strategy_cb.currentIndex()],
            "n_random_trials":    self._n_trials_spin.value(),
            "n_workers":          self._workers_spin.value(),
            "objective":          self._obj_cb.currentText().lower().replace(" ", "_"),
            "cost_per_side":      fee_map[self._fee_cb.currentIndex()],
            "confluence_mode":    conf_map[self._conf_cb.currentIndex()],
            "orchestration_mode": orch_map[self._orch_cb.currentIndex()],
            "hmm_confidence_min": hmm_conf_map[self._hmm_conf_cb.currentIndex()],
        }

    def set_running(self, running: bool):
        self._start_btn.setEnabled(not running)
        self._cancel_btn.setEnabled(running)


# ─────────────────────────────────────────────────────────────────────────────
# Period Panel  (Phase 1)
# ─────────────────────────────────────────────────────────────────────────────

_DEFAULT_START = "2022-03-22"
_DEFAULT_END   = "2026-03-21"


class _PeriodPanel(QWidget):
    """Date-range selector with quick presets and live duration display."""

    period_changed = Signal(str, str)   # date_start, date_end ISO strings

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build()

    def _build(self):
        card, vlay = _card("📅  Backtest Period")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)

        # ── Quick presets ────────────────────────────────────────────────
        preset_row = QHBoxLayout()
        preset_row.addWidget(_label("Preset:", color=_DIM))
        for label, months in [("6m", 6), ("1y", 12), ("2y", 24), ("4y", 48)]:
            btn = QPushButton(label)
            btn.setFixedWidth(38)
            btn.setStyleSheet(
                f"background:#1a3555; color:{_TEXT}; border-radius:3px; padding:3px;"
                " font-size:12px;"
            )
            btn.clicked.connect(lambda _, m=months: self._apply_preset(m))
            preset_row.addWidget(btn)
        preset_row.addStretch()
        vlay.addLayout(preset_row)

        # ── Date pickers ─────────────────────────────────────────────────
        picker_grid = QGridLayout()
        picker_grid.setHorizontalSpacing(8)
        picker_grid.setVerticalSpacing(4)

        picker_grid.addWidget(_label("Start:", color=_TEXT), 0, 0)
        self._start_edit = QDateEdit()
        self._start_edit.setCalendarPopup(True)
        self._start_edit.setDisplayFormat("yyyy-MM-dd")
        self._start_edit.setDate(QDate.fromString(_DEFAULT_START, "yyyy-MM-dd"))
        self._start_edit.setStyleSheet(
            f"background:{_DARK}; color:{_TEXT}; border:1px solid #444; border-radius:3px;"
            " padding:2px 4px;"
        )
        picker_grid.addWidget(self._start_edit, 0, 1)

        picker_grid.addWidget(_label("End:", color=_TEXT), 1, 0)
        self._end_edit = QDateEdit()
        self._end_edit.setCalendarPopup(True)
        self._end_edit.setDisplayFormat("yyyy-MM-dd")
        self._end_edit.setDate(QDate.fromString(_DEFAULT_END, "yyyy-MM-dd"))
        self._end_edit.setStyleSheet(
            f"background:{_DARK}; color:{_TEXT}; border:1px solid #444; border-radius:3px;"
            " padding:2px 4px;"
        )
        picker_grid.addWidget(self._end_edit, 1, 1)
        vlay.addLayout(picker_grid)

        # ── Duration display ─────────────────────────────────────────────
        self._dur_lbl = _label("", color=_DIM)
        self._dur_lbl.setStyleSheet(f"color:{_DIM}; font-size:11px; padding-top:2px;")
        vlay.addWidget(self._dur_lbl)

        self._start_edit.dateChanged.connect(self._on_changed)
        self._end_edit.dateChanged.connect(self._on_changed)
        self._on_changed()   # initialise duration label

    def _apply_preset(self, months: int):
        from PySide6.QtCore import QDate
        end   = QDate.fromString(_DEFAULT_END, "yyyy-MM-dd")
        start = end.addMonths(-months)
        self._start_edit.setDate(start)
        self._end_edit.setDate(end)

    def _on_changed(self):
        start = self._start_edit.date()
        end   = self._end_edit.date()
        if start >= end:
            self._dur_lbl.setText("⚠ Start must be before End")
            self._dur_lbl.setStyleSheet(f"color:{_RED}; font-size:11px;")
            return
        days  = start.daysTo(end)
        years = days / 365.25
        self._dur_lbl.setText(f"Duration: {years:.1f} years  ({days:,} days)")
        self._dur_lbl.setStyleSheet(f"color:{_DIM}; font-size:11px;")
        self.period_changed.emit(
            start.toString("yyyy-MM-dd"),
            end.toString("yyyy-MM-dd"),
        )

    @property
    def date_start(self) -> str:
        return self._start_edit.date().toString("yyyy-MM-dd")

    @property
    def date_end(self) -> str:
        return self._end_edit.date().toString("yyyy-MM-dd")

    def is_valid(self) -> bool:
        return self._start_edit.date() < self._end_edit.date()


# ─────────────────────────────────────────────────────────────────────────────
# Asset Panel  (Phase 1)
# ─────────────────────────────────────────────────────────────────────────────

_ALL_SYMBOLS     = ["BTC/USDT", "SOL/USDT", "ETH/USDT", "XRP/USDT", "BNB/USDT"]
_DEFAULT_SYMBOLS = ["BTC/USDT", "SOL/USDT", "ETH/USDT"]


class _AssetPanel(QWidget):
    """Multi-asset selector with checkboxes and Add Asset workflow."""

    assets_changed = Signal(list)   # list of selected symbol strings

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build()

    def _build(self):
        card, vlay = _card("📊  Assets")
        # Zero spacing — thin separators inserted manually between rows
        vlay.setSpacing(0)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)

        # ── Checkboxes ───────────────────────────────────────────────────
        self._checkboxes: dict[str, QCheckBox] = {}
        for sym in _ALL_SYMBOLS:
            cb = QCheckBox(sym)
            cb.setChecked(sym in _DEFAULT_SYMBOLS)
            # Style: transparent bg inherits the _DARK row bg, padded like a table cell
            cb.setStyleSheet(
                f"QCheckBox {{ color:{_TEXT}; font-size:13px; padding:5px 6px;"
                f" background:{_DARK}; }}"
                f" QCheckBox::indicator {{ width:14px; height:14px; }}"
            )
            cb.toggled.connect(self._on_changed)
            self._checkboxes[sym] = cb
            vlay.addWidget(cb)
            vlay.addWidget(_hsep())

        vlay.addSpacing(6)

        # ── Select all / clear all ───────────────────────────────────────
        sel_row = QHBoxLayout()
        all_btn = QPushButton("Select All")
        all_btn.setStyleSheet(
            f"background:#1a3555; color:{_TEXT}; border-radius:3px; padding:3px; font-size:12px;"
        )
        all_btn.clicked.connect(lambda: self._set_all(True))
        sel_row.addWidget(all_btn)
        clear_btn = QPushButton("Clear All")
        clear_btn.setStyleSheet(
            f"background:#2a1515; color:{_TEXT}; border-radius:3px; padding:3px; font-size:12px;"
        )
        clear_btn.clicked.connect(lambda: self._set_all(False))
        sel_row.addWidget(clear_btn)
        vlay.addLayout(sel_row)

        # ── Add asset ────────────────────────────────────────────────────
        vlay.addSpacing(4)
        add_hdr = _label("Add asset (e.g. DOGE/USDT):", color=_DIM)
        add_hdr.setStyleSheet(f"color:{_DIM}; font-size:11px;")
        vlay.addWidget(add_hdr)

        add_row = QHBoxLayout()
        self._add_input = QLineEdit()
        self._add_input.setPlaceholderText("SYMBOL/USDT")
        self._add_input.setStyleSheet(
            f"background:{_DARK}; color:{_TEXT}; border:1px solid #444; border-radius:3px;"
            " padding:2px 6px; font-size:12px;"
        )
        add_row.addWidget(self._add_input)

        self._add_btn = QPushButton("Validate")
        self._add_btn.setFixedWidth(70)
        self._add_btn.setStyleSheet(
            f"background:{_BLUE}; color:white; border-radius:3px; padding:3px; font-size:12px;"
        )
        self._add_btn.clicked.connect(self._on_validate_add)
        add_row.addWidget(self._add_btn)
        vlay.addLayout(add_row)

        self._add_status_lbl = _label("", color=_DIM)
        self._add_status_lbl.setStyleSheet(f"color:{_DIM}; font-size:11px;")
        self._add_status_lbl.setWordWrap(True)
        vlay.addWidget(self._add_status_lbl)

    def _set_all(self, state: bool):
        for cb in self._checkboxes.values():
            cb.setChecked(state)

    def _on_changed(self):
        self.assets_changed.emit(self.selected_symbols())

    def selected_symbols(self) -> list[str]:
        return [sym for sym, cb in self._checkboxes.items() if cb.isChecked()]

    def add_symbol(self, symbol: str):
        """Add a new checkbox for a symbol not in the default list."""
        if symbol in self._checkboxes:
            self._checkboxes[symbol].setChecked(True)
            return
        # Insert checkbox + separator before the spacing/sel_row section
        layout = self.layout().itemAt(0).widget().layout()
        cb = QCheckBox(symbol)
        cb.setChecked(True)
        cb.setStyleSheet(
            f"QCheckBox {{ color:{_TEXT}; font-size:13px; padding:5px 6px;"
            f" background:{_DARK}; }}"
            f" QCheckBox::indicator {{ width:14px; height:14px; }}"
        )
        cb.toggled.connect(self._on_changed)
        self._checkboxes[symbol] = cb
        # Each original symbol took 2 items (checkbox + separator) in the layout
        insert_pos = (len(self._checkboxes) - 1) * 2
        layout.insertWidget(insert_pos, cb)
        layout.insertWidget(insert_pos + 1, _hsep())
        self._on_changed()

    def _on_validate_add(self):
        """Validate that the entered symbol exists on Bybit (background)."""
        sym = self._add_input.text().strip().upper()
        if not sym or "/" not in sym:
            self._add_status_lbl.setText("⚠ Enter a symbol like DOGE/USDT")
            self._add_status_lbl.setStyleSheet(f"color:{_AMBER}; font-size:11px;")
            return

        self._add_btn.setEnabled(False)
        self._add_status_lbl.setText("Checking exchange…")
        self._add_status_lbl.setStyleSheet(f"color:{_DIM}; font-size:11px;")

        # Run in thread to avoid blocking UI
        from research.engine.data_manager import DataManager
        import threading

        def _check():
            dm = DataManager()
            ok, reason = dm.validate_exchange_symbol(sym)
            # Return to main thread via QTimer
            if ok:
                QTimer.singleShot(0, lambda: self._on_validate_ok(sym))
            else:
                QTimer.singleShot(0, lambda: self._on_validate_fail(reason))

        t = threading.Thread(target=_check, daemon=True)
        t.start()

    def _on_validate_ok(self, symbol: str):
        self._add_btn.setEnabled(True)
        self._add_status_lbl.setText(f"✅ {symbol} confirmed on Bybit. Data must be fetched before use.")
        self._add_status_lbl.setStyleSheet(f"color:{_GREEN}; font-size:11px;")
        self.add_symbol(symbol)

    def _on_validate_fail(self, reason: str):
        self._add_btn.setEnabled(True)
        self._add_status_lbl.setText(f"❌ {reason}")
        self._add_status_lbl.setStyleSheet(f"color:{_RED}; font-size:11px;")


# ─────────────────────────────────────────────────────────────────────────────
# Data Status Panel  (Phase 2 / 4)
# ─────────────────────────────────────────────────────────────────────────────

class DataWorkerThread(QThread):
    """Background thread for data registry scan and fetch operations."""
    progress_sig = Signal(float, str)   # pct [0-1], message
    done_sig     = Signal(bool, str)    # success, message

    def __init__(self, mode: str, symbols: list, date_start: str, date_end: str,
                 target_symbol: str = "", parent=None):
        super().__init__(parent)
        self._mode    = mode    # "check" | "fetch"
        self._symbols = symbols
        self._date_start = date_start
        self._date_end   = date_end
        self._target     = target_symbol

    def run(self):
        try:
            from research.engine.data_manager import DataManager
            dm = DataManager()

            if self._mode == "check":
                self.progress_sig.emit(0.1, "Scanning local data files…")
                dm.refresh_registry(
                    progress_cb=lambda sym, done, total: self.progress_sig.emit(
                        done / total, f"Scanned {sym} ({done}/{total})"
                    )
                )
                result = dm.check(self._symbols, self._date_start, self._date_end)
                self.done_sig.emit(result.ok, result.summary())

            elif self._mode == "fetch":
                ok, msg = dm.add_asset(
                    self._target,
                    date_start   = self._date_start,
                    date_end     = self._date_end,
                    progress_cb  = lambda pct, msg: self.progress_sig.emit(pct, msg),
                )
                self.done_sig.emit(ok, msg)
        except Exception as exc:
            logger.exception("DataWorkerThread error")
            self.done_sig.emit(False, str(exc))


class _DataStatusPanel(QWidget):
    """
    Shows per-symbol data availability and provides Check / Download buttons.
    """

    check_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build()

    def _build(self):
        card, vlay = _card("💾  Data Status")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)

        # ── Status table ─────────────────────────────────────────────────
        _COLS = ["Symbol", "Status", "Coverage", "Rows"]
        self._table = QTableWidget(0, len(_COLS))
        self._table.setHorizontalHeaderLabels(_COLS)
        # Column proportion 1 : 1 : 2 : 1
        # Symbol(0), Status(1), Rows(3) are Fixed at equal width;
        # Coverage(2) is Stretch and takes all remaining space (≈ 2× the others).
        _hdr = self._table.horizontalHeader()
        _hdr.setSectionResizeMode(0, QHeaderView.Fixed)   # Symbol
        _hdr.setSectionResizeMode(1, QHeaderView.Fixed)   # Status
        _hdr.setSectionResizeMode(2, QHeaderView.Stretch) # Coverage — fills remaining
        _hdr.setSectionResizeMode(3, QHeaderView.Fixed)   # Rows
        self._table.setColumnWidth(0, 72)   # Symbol
        self._table.setColumnWidth(1, 72)   # Status
        self._table.setColumnWidth(3, 52)   # Rows
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionMode(QAbstractItemView.NoSelection)
        self._table.setMaximumHeight(140)
        self._table.setStyleSheet(
            f"background:{_DARK}; color:{_TEXT}; gridline-color:#2d2d2d;"
            " font-size:12px; border:none;"
        )
        self._table.verticalHeader().setVisible(False)
        vlay.addWidget(self._table)

        # ── Buttons ──────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        self._check_btn = QPushButton("🔍 Check Data")
        self._check_btn.setStyleSheet(
            f"background:{_BLUE}; color:white; font-weight:bold;"
            " border-radius:3px; padding:5px; font-size:12px;"
        )
        self._check_btn.clicked.connect(self.check_requested)
        btn_row.addWidget(self._check_btn)
        vlay.addLayout(btn_row)

        # ── Status line ───────────────────────────────────────────────────
        self._status_lbl = _label("Click Check Data to verify availability.", color=_DIM)
        self._status_lbl.setStyleSheet(f"color:{_DIM}; font-size:11px;")
        self._status_lbl.setWordWrap(True)
        vlay.addWidget(self._status_lbl)

    def update_table(self, rows: list[dict]):
        self._table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            status = row.get("status", "—")
            color  = _GREEN if status == "Ready" else (_AMBER if "Missing" in status else _RED)
            items = [
                (row.get("symbol", ""), _TEXT),
                (status, color),
                (f"{row.get('first_date','—')} → {row.get('last_date','—')}", _TEXT),
                (f"{row.get('rows_30m', 0):,}", _TEXT),
            ]
            for j, (val, col) in enumerate(items):
                item = QTableWidgetItem(val)
                item.setForeground(QColor(col))
                item.setTextAlignment(Qt.AlignCenter)
                self._table.setItem(i, j, item)

    def set_status(self, msg: str, ok: bool | None = None):
        self._status_lbl.setText(msg)
        if ok is True:
            self._status_lbl.setStyleSheet(f"color:{_GREEN}; font-size:11px;")
        elif ok is False:
            self._status_lbl.setStyleSheet(f"color:{_RED}; font-size:11px;")
        else:
            self._status_lbl.setStyleSheet(f"color:{_DIM}; font-size:11px;")

    def set_checking(self, active: bool):
        self._check_btn.setEnabled(not active)
        self._check_btn.setText("Checking…" if active else "🔍 Check Data")


# ─────────────────────────────────────────────────────────────────────────────
# Progress Panel
# ─────────────────────────────────────────────────────────────────────────────

class _ProgressPanel(QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build()

    def _build(self):
        card, vlay = _card("◎  Progress")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)

        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.setStyleSheet(
            f"QProgressBar {{ background:{_DARK}; border:1px solid #444;"
            " border-radius:4px; text-align:center; color:{_TEXT}; }}"
            f"QProgressBar::chunk {{ background:{_BLUE}; border-radius:3px; }}"
        )
        vlay.addWidget(self._bar)

        stats_row = QHBoxLayout()
        self._trials_lbl = _label("0 / 0 trials", color=_TEXT)
        stats_row.addWidget(self._trials_lbl)
        stats_row.addStretch()
        self._best_lbl = _label("Best PF: —", bold=True, color=_GREEN)
        stats_row.addWidget(self._best_lbl)
        vlay.addLayout(stats_row)

        self._msg_lbl = _label("Idle", color=_TEXT)
        vlay.addWidget(self._msg_lbl)

        # ── Cache status row ──────────────────────────────────────────────────
        cache_row = QHBoxLayout()
        self._cache_lbl = _label("Indicator cache: —", color=_DIM, size=11)
        self._cache_lbl.setStyleSheet(f"color:{_DIM}; font-size:11px;")
        cache_row.addWidget(self._cache_lbl)
        cache_row.addStretch()
        from PySide6.QtWidgets import QPushButton
        self._clear_cache_btn = QPushButton("🗑 Clear Cache")
        self._clear_cache_btn.setFixedHeight(22)
        self._clear_cache_btn.setStyleSheet(
            f"QPushButton {{ background:#3a2020; color:#cc6666; border:1px solid #664444;"
            " border-radius:3px; font-size:11px; padding:0 8px; }}"
            "QPushButton:hover { background:#4a2a2a; }"
            "QPushButton:disabled { color:#555; border-color:#333; }"
        )
        self._clear_cache_btn.clicked.connect(self._on_clear_cache)
        cache_row.addWidget(self._clear_cache_btn)
        vlay.addLayout(cache_row)

        # Mini leaderboard (top-5)
        self._mini_table = QTableWidget(5, 3)
        self._mini_table.setHorizontalHeaderLabels(["Rank", "PF", "n"])
        self._mini_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._mini_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._mini_table.setSelectionMode(QAbstractItemView.NoSelection)
        self._mini_table.setMaximumHeight(130)
        self._mini_table.setStyleSheet(
            f"background:{_DARK}; color:{_TEXT}; gridline-color:#333;"
            " font-size:13px; border:none;"
        )
        self._mini_table.verticalHeader().setVisible(False)
        vlay.addWidget(self._mini_table)

    @Slot(bool)
    def set_indeterminate(self, active: bool):
        """Switch between pulsing animation (active=True) and normal bar."""
        if active:
            self._bar.setRange(0, 0)   # Qt pulse animation
        else:
            self._bar.setRange(0, 100)

    def update_progress(self, completed: int, total: int, best_pf: float, msg: str):
        if self._bar.maximum() != 0:   # determinate mode
            pct = int(completed / total * 100) if total else 0
            self._bar.setValue(pct)

        # For baseline (total ≤ 10) show phases; for sweeps show trial counter
        if total > 10:
            self._trials_lbl.setText(f"{completed} / {total} trials")
        else:
            phase_txt = f"Phase {completed}/{total}" if total > 0 else "—"
            self._trials_lbl.setText(phase_txt)

        self._best_lbl.setText(f"Best PF: {best_pf:.4f}" if best_pf else "Best PF: —")
        self._msg_lbl.setText(msg)

    def update_leaderboard(self, rows: list[dict]):
        """rows: list of {rank, profit_factor, n_trades}"""
        for i, row in enumerate(rows[:5]):
            self._mini_table.setItem(i, 0, QTableWidgetItem(str(i + 1)))
            self._mini_table.setItem(i, 1, QTableWidgetItem(f"{row.get('profit_factor',0):.4f}"))
            self._mini_table.setItem(i, 2, QTableWidgetItem(str(row.get('n_trades', 0))))

    def update_cache_info(self, info: dict):
        """Update the cache status label with HIT/MISS and size info."""
        if not info:
            return
        hits   = info.get("cache_hits",    0)
        misses = info.get("cache_misses",  0)
        size   = info.get("cache_size_mb", 0.0)
        total  = hits + misses
        if total == 0:
            return
        hit_pct = int(hits / total * 100)
        color   = _GREEN if hit_pct >= 80 else (_AMBER if hit_pct >= 50 else _TEXT)
        self._cache_lbl.setText(
            f"Cache: {hits}/{total} HIT ({hit_pct}%) | {size:.0f} MB"
        )
        self._cache_lbl.setStyleSheet(f"color:{color}; font-size:11px;")

    def _on_clear_cache(self):
        """Delete all cached indicator/regime files (blocking, fast)."""
        try:
            from research.engine.backtest_runner import BacktestRunner
            n = BacktestRunner.clear_cache()
            self._cache_lbl.setText(f"Cache cleared ({n} files deleted)")
            self._cache_lbl.setStyleSheet(f"color:{_AMBER}; font-size:11px;")
        except Exception as exc:
            self._cache_lbl.setText(f"Clear failed: {exc}")
            self._cache_lbl.setStyleSheet(f"color:#cc6666; font-size:11px;")

    def reset(self):
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._trials_lbl.setText("—")
        self._best_lbl.setText("Best PF: —")
        self._msg_lbl.setStyleSheet(f"color:{_TEXT};")
        self._msg_lbl.setText("Idle")


# ─────────────────────────────────────────────────────────────────────────────
# Results Panel
# ─────────────────────────────────────────────────────────────────────────────

_RESULT_COLS = ["#", "PF", "n", "WR%", "CAGR%", "MaxDD%", "PBL_PF", "SLC_PF"]


class _ResultsPanel(QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[dict] = []
        self._build()

    def _build(self):
        card, vlay = _card("◈  Results")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)

        # Filter bar
        filter_row = QHBoxLayout()
        filter_row.addWidget(_label("Min PF:", color=_TEXT))
        self._min_pf = QDoubleSpinBox()
        self._min_pf.setRange(0.0, 10.0)
        self._min_pf.setValue(0.0)
        self._min_pf.setSingleStep(0.1)
        self._min_pf.setStyleSheet(
            f"background:{_DARK}; color:{_TEXT}; border:1px solid #444; width:60px;"
        )
        self._min_pf.valueChanged.connect(self._apply_filter)
        filter_row.addWidget(self._min_pf)
        filter_row.addStretch()
        self._count_lbl = _label("0 trials", color=_TEXT)
        filter_row.addWidget(self._count_lbl)
        vlay.addLayout(filter_row)

        self._table = QTableWidget(0, len(_RESULT_COLS))
        self._table.setHorizontalHeaderLabels(_RESULT_COLS)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSortingEnabled(True)
        self._table.setStyleSheet(
            f"background:{_DARK}; color:{_TEXT}; gridline-color:#333;"
            " font-size:13px; border:none;"
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        vlay.addWidget(self._table)

    def add_result(self, result: dict):
        self._rows.append(result)
        self._apply_filter()

    def _apply_filter(self):
        min_pf = self._min_pf.value()
        filtered = [r for r in self._rows if r.get("profit_factor", 0) >= min_pf]
        filtered.sort(key=lambda r: r.get("profit_factor", 0), reverse=True)

        self._table.setRowCount(len(filtered))
        for i, row in enumerate(filtered):
            vals = [
                str(i + 1),
                f"{row.get('profit_factor', 0):.4f}",
                str(row.get("n_trades", 0)),
                f"{row.get('win_rate', 0) * 100:.1f}",
                f"{row.get('cagr', 0) * 100:.1f}",
                f"{row.get('max_drawdown', 0) * 100:.1f}",
                f"{row.get('pbl_pf', 0):.4f}",
                f"{row.get('slc_pf', 0):.4f}",
            ]
            for j, v in enumerate(vals):
                item = QTableWidgetItem(v)
                item.setTextAlignment(Qt.AlignCenter)
                # Colour-code PF column
                if j == 1:
                    pf = row.get("profit_factor", 0)
                    color = _GREEN if pf >= 1.3 else (_AMBER if pf >= 1.1 else _RED)
                    item.setForeground(QColor(color))
                self._table.setItem(i, j, item)

        self._count_lbl.setText(f"{len(filtered)} trials")

    def clear(self):
        self._rows.clear()
        self._table.setRowCount(0)
        self._count_lbl.setText("0 trials")


# ─────────────────────────────────────────────────────────────────────────────
# Validation Panel (IS/OOS)
# ─────────────────────────────────────────────────────────────────────────────

class _ValidationPanel(QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build()

    def _build(self):
        card, vlay = _card("⊕  Validation (IS / OOS)")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)

        # IS / OOS period info — styled rows
        periods_frame = QFrame()
        periods_frame.setStyleSheet(
            f"background:{_PANEL}; border:1px solid #2a4a7a; border-radius:4px;"
        )
        periods_lay = QVBoxLayout(periods_frame)
        periods_lay.setContentsMargins(10, 8, 10, 8)
        periods_lay.setSpacing(4)

        lbl_is = _label("IS period:  2022-03-22 → 2025-09-21  (3.5 years)", bold=False, color=_TEXT)
        lbl_is.setStyleSheet(f"color:{_TEXT}; font-size:13px; padding-left:4px;")
        periods_lay.addWidget(lbl_is)

        lbl_oos = _label("OOS period:  2025-09-22 → 2026-03-21  (6 months)", bold=False, color=_AMBER)
        lbl_oos.setStyleSheet(f"color:{_AMBER}; font-size:13px; padding-left:4px;")
        periods_lay.addWidget(lbl_oos)

        vlay.addWidget(periods_frame)

        hint = QLabel("Select a candidate from Results and click Run OOS to evaluate on the held-out period.")
        hint.setStyleSheet(f"color:{_DIM}; font-size:12px; padding: 4px 6px;")
        hint.setWordWrap(True)
        vlay.addWidget(hint)

        self._oos_btn = QPushButton("▶  Run OOS on Selected Candidate")
        self._oos_btn.setStyleSheet(
            f"background:{_AMBER}; color:white; font-weight:bold;"
            " padding:6px; border-radius:4px;"
        )
        vlay.addWidget(self._oos_btn)

        self._oos_result = QTextEdit()
        self._oos_result.setReadOnly(True)
        self._oos_result.setMaximumHeight(100)
        self._oos_result.setStyleSheet(
            f"background:{_DARK}; color:{_TEXT}; border:none; font-size:13px;"
        )
        self._oos_result.setPlainText("OOS not yet run.")
        vlay.addWidget(self._oos_result)

        self._oos_btn.clicked.connect(self._run_oos)

    def _run_oos(self):
        self._oos_result.setPlainText("OOS validation — running in background…")
        # Placeholder: in a full implementation, fire a SweepWorkerThread in "oos" mode


# ─────────────────────────────────────────────────────────────────────────────
# Main Research Lab Page
# ─────────────────────────────────────────────────────────────────────────────

class ResearchLabPage(QWidget):
    """
    First-class NexusTrader Research Lab page.

    Registration: main_window.py NAV_ITEMS + page_map + _load_pages().
    Key: "research_lab"
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state        = _LabState()
        self._worker:      Optional[SweepWorkerThread] = None
        self._data_worker: Optional[DataWorkerThread]  = None
        self._build()
        self._restore_baseline_cache()
        # Kick off a background registry scan so the data table is populated on load
        QTimer.singleShot(1500, self._auto_check_data)

    # ─────────────────────────────────────────────────────────────────────────
    # Baseline cache persistence
    # ─────────────────────────────────────────────────────────────────────────

    def _save_baseline_cache(self, summary: dict):
        """Persist the last baseline result to disk so it survives restarts."""
        try:
            payload = {
                "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "summary":  summary,
            }
            _BASELINE_CACHE.write_text(json.dumps(payload, indent=2))
        except Exception as exc:
            logger.warning("Could not save baseline cache: %s", exc)

    def _restore_baseline_cache(self):
        """On startup, reload the last baseline result and repopulate the UI."""
        if not _BASELINE_CACHE.exists():
            return
        try:
            payload  = json.loads(_BASELINE_CACHE.read_text())
            summary  = payload.get("summary", {})
            saved_at = payload.get("saved_at", "unknown")
            passed   = summary.get("passed", False)

            self._state.baseline_status  = "PASS" if passed else "FAIL"
            self._state.baseline_metrics = summary

            # Restore panel — annotate with the saved timestamp
            self._baseline_panel.set_result(summary, saved_at=saved_at)
            status_msg = (
                f"Baseline PASS ✅ (from {saved_at})"
                if passed else
                f"Baseline FAIL ❌ (from {saved_at})"
            )
            self._progress_panel.update_progress(4, 4, 0.0, status_msg)
            logger.info("Baseline cache restored from %s (status=%s)", saved_at,
                        self._state.baseline_status)
        except Exception as exc:
            logger.warning("Could not restore baseline cache: %s", exc)

    # ─────────────────────────────────────────────────────────────────────────
    # Layout
    # ─────────────────────────────────────────────────────────────────────────

    def _build(self):
        self.setStyleSheet(f"background:{_DARK}; color:{_TEXT}; font-size:13px;")

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(8)

        # ── Header ─────────────────────────────────────────────────────
        hdr = QHBoxLayout()
        title = _label("⚗  Research Lab", bold=True, size=16)
        hdr.addWidget(title)
        hdr.addStretch()
        self._exp_lbl = _label("No experiment loaded", color=_DIM)
        hdr.addWidget(self._exp_lbl)
        root.addLayout(hdr)

        # ── Horizontal splitter (left config | right results) ──────────
        splitter = QSplitter(Qt.Horizontal)
        splitter.setStyleSheet("QSplitter::handle { background: #2a4a7a; width: 2px; }")
        root.addWidget(splitter, 1)          # stretch=1 → fills all remaining height

        # ── LEFT — configuration ───────────────────────────────────────
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setStyleSheet(
            f"background:{_DARK}; border:none;"
        )
        left_widget = QWidget()
        left_widget.setStyleSheet(f"background:{_DARK};")
        left_layout = QVBoxLayout(left_widget)
        left_layout.setSpacing(8)
        left_layout.setContentsMargins(0, 0, 4, 0)

        self._baseline_panel = _BaselinePanel()
        self._baseline_panel.baseline_requested.connect(self._on_run_baseline)
        left_layout.addWidget(self._baseline_panel)

        self._period_panel = _PeriodPanel()
        self._period_panel.period_changed.connect(self._on_period_changed)
        left_layout.addWidget(self._period_panel)

        self._asset_panel = _AssetPanel()
        self._asset_panel.assets_changed.connect(self._on_assets_changed)
        left_layout.addWidget(self._asset_panel)

        self._data_status_panel = _DataStatusPanel()
        self._data_status_panel.check_requested.connect(self._on_check_data)
        left_layout.addWidget(self._data_status_panel)

        # ── Strategy selection (Phase 2 — unified engine) ─────────────────
        self._strategy_panel = _StrategyPanel()
        self._strategy_panel.mode_changed.connect(self._on_strategy_mode_changed)
        left_layout.addWidget(self._strategy_panel)

        self._param_panel = _ParameterPanel()
        left_layout.addWidget(self._param_panel)

        self._search_panel = _SearchPanel()
        self._search_panel.start_requested.connect(self._on_start_sweep)
        self._search_panel.cancel_requested.connect(self._on_cancel)
        left_layout.addWidget(self._search_panel)

        left_layout.addStretch()
        left_scroll.setWidget(left_widget)
        left_scroll.setMinimumWidth(440)
        splitter.addWidget(left_scroll)

        # ── RIGHT — results ────────────────────────────────────────────
        right_widget = QWidget()
        right_widget.setStyleSheet(f"background:{_DARK};")
        right_layout = QVBoxLayout(right_widget)
        right_layout.setSpacing(8)
        right_layout.setContentsMargins(4, 0, 0, 0)

        self._progress_panel = _ProgressPanel()
        right_layout.addWidget(self._progress_panel)

        results_splitter = QSplitter(Qt.Horizontal)
        results_splitter.setStyleSheet("QSplitter::handle { background: #2a4a7a; width: 2px; }")

        self._results_panel = _ResultsPanel()
        results_splitter.addWidget(self._results_panel)

        self._validation_panel = _ValidationPanel()
        results_splitter.addWidget(self._validation_panel)
        results_splitter.setSizes([700, 300])

        right_layout.addWidget(results_splitter, 1)  # results table gets all spare height
        splitter.addWidget(right_widget)
        splitter.setSizes([450, 800])
        splitter.setStretchFactor(0, 0)   # left: fixed width
        splitter.setStretchFactor(1, 1)   # right: expands with window

    # ─────────────────────────────────────────────────────────────────────────
    # Slots
    # ─────────────────────────────────────────────────────────────────────────

    # ─────────────────────────────────────────────────────────────────────────
    # Period / Asset / Data slots
    # ─────────────────────────────────────────────────────────────────────────

    @Slot(str, str)
    def _on_period_changed(self, date_start: str, date_end: str):
        self._state.date_start = date_start
        self._state.date_end   = date_end

    @Slot(list)
    def _on_assets_changed(self, symbols: list):
        self._state.selected_symbols = symbols

    @Slot(str, list)
    def _on_strategy_mode_changed(self, mode: str, subset: list):
        """User changed the strategy selection — update state and rebuild params."""
        self._state.backtest_mode   = mode
        self._state.strategy_subset = subset
        # Rebuild parameter rows to show only relevant params for the new mode
        self._param_panel.rebuild(mode)
        # Update the experiment label to reflect the active strategy
        label_map = {k: lbl for k, lbl, _ in _STRATEGY_OPTIONS}
        lbl = label_map.get(mode, mode)
        self._exp_lbl.setText(f"Strategy: {lbl}")

    @Slot()
    def _on_check_data(self):
        """Run DataManager.check() in background and update the data status table."""
        if self._data_worker and self._data_worker.isRunning():
            return
        self._data_status_panel.set_checking(True)
        self._data_status_panel.set_status("Scanning…", None)

        self._data_worker = DataWorkerThread(
            mode       = "check",
            symbols    = self._state.selected_symbols,
            date_start = self._state.date_start,
            date_end   = self._state.date_end,
        )
        self._data_worker.done_sig.connect(self._on_data_check_done)
        self._data_worker.start()

    @Slot(bool, str)
    def _on_data_check_done(self, ok: bool, msg: str):
        self._data_status_panel.set_checking(False)
        self._data_status_panel.set_status(msg, ok)

        # Refresh the table from the registry
        try:
            from research.engine.data_registry import DataRegistry
            reg = DataRegistry()
            reg.load()
            rows = reg.summary_table()
            self._data_status_panel.update_table(rows)
        except Exception as exc:
            logger.warning("Could not refresh data table: %s", exc)

    def _auto_check_data(self):
        """Silently populate the data table from cached registry on startup."""
        try:
            from research.engine.data_registry import DataRegistry, _REGISTRY_PATH
            reg = DataRegistry()
            if _REGISTRY_PATH.exists():
                reg.load()
            else:
                reg.build()
                reg.save()
            rows = reg.summary_table()
            self._data_status_panel.update_table(rows)
            logger.info("DataRegistry auto-loaded %d rows", len(rows))
        except Exception as exc:
            logger.warning("Auto data-check failed: %s", exc)

    @Slot()
    def _on_run_baseline(self):
        if self._state.sweep_running:
            return
        self._state.baseline_status = "RUNNING"
        self._baseline_panel.set_running()
        self._progress_panel.reset()
        self._progress_panel.update_progress(0, 100, 0.0, "Loading data…")

        self._worker = SweepWorkerThread("baseline", self._state)
        self._worker.progress.connect(self._on_progress)
        self._worker.indeterminate.connect(self._progress_panel.set_indeterminate)
        self._worker.finished_ok.connect(self._on_baseline_done)
        self._worker.error_signal.connect(self._on_error)
        # Session 45: wire cache info to progress panel display
        self._worker.cache_info_sig.connect(self._progress_panel.update_cache_info)
        self._worker.start()

    @Slot()
    def _on_start_sweep(self):
        if self._state.sweep_running:
            return

        # Sync period + assets from UI into state
        if self._period_panel.is_valid():
            self._state.date_start = self._period_panel.date_start
            self._state.date_end   = self._period_panel.date_end
        else:
            self._progress_panel.update_progress(
                0, 1, 0.0, "⚠ Invalid period: start must be before end."
            )
            return

        self._state.selected_symbols = self._asset_panel.selected_symbols()
        if not self._state.selected_symbols:
            self._progress_panel.update_progress(
                0, 1, 0.0, "⚠ No assets selected. Select at least one asset."
            )
            return

        if not self._param_panel.has_optimize():
            self._progress_panel.update_progress(
                0, 1, 0.0,
                "⚠ No parameters set to OPTIMIZE. Toggle at least one to OPTIMIZE."
            )
            return

        modes, values = self._param_panel.get_state()
        search = self._search_panel.get_state()

        self._state.param_modes    = modes
        self._state.param_values   = values
        self._state.search_strategy= search["search_strategy"]
        self._state.n_random_trials= search["n_random_trials"]
        self._state.n_workers      = search["n_workers"]
        self._state.cost_per_side      = search["cost_per_side"]
        self._state.confluence_mode    = search.get("confluence_mode", "none")
        self._state.orchestration_mode = search.get("orchestration_mode", "naive")
        self._state.hmm_confidence_min = float(search.get("hmm_confidence_min", 0.0))

        from research.engine.experiment_store import ExperimentStore
        exp_id = ExperimentStore.new_id()
        self._state.current_experiment = exp_id
        self._exp_lbl.setText(f"Experiment: {exp_id}")

        self._state.sweep_running = True
        self._results_panel.clear()
        self._progress_panel.reset()
        self._search_panel.set_running(True)

        self._worker = SweepWorkerThread("sweep", self._state)
        self._worker.progress.connect(self._on_progress)
        self._worker.indeterminate.connect(self._progress_panel.set_indeterminate)
        self._worker.trial_done.connect(self._on_trial_done)
        self._worker.finished_ok.connect(self._on_sweep_done)
        self._worker.error_signal.connect(self._on_error)
        self._worker.start()

    @Slot()
    def _on_cancel(self):
        if self._worker:
            self._worker.cancel()
        self._state.sweep_running = False
        self._search_panel.set_running(False)

    @Slot(int, int, float, str)
    def _on_progress(self, completed: int, total: int, best_pf: float, msg: str):
        self._progress_panel.update_progress(completed, total, best_pf, msg)

    @Slot(dict)
    def _on_trial_done(self, result: dict):
        self._results_panel.add_result(result)
        # Update mini leaderboard
        rows = sorted(
            [r for r in self._results_panel._rows if r.get("status") == "ok"],
            key=lambda r: r.get("profit_factor", 0),
            reverse=True,
        )[:5]
        self._progress_panel.update_leaderboard(rows)

    @Slot(dict)
    def _on_baseline_done(self, summary: dict):
        passed = summary.get("passed", False)
        self._state.baseline_status = "PASS" if passed else "FAIL"
        self._state.baseline_metrics = summary
        self._baseline_panel.set_result(summary)
        self._progress_panel.update_progress(4, 4, 0.0,
            "Baseline PASS ✅" if passed else "Baseline FAIL ❌ — optimization blocked")
        self._save_baseline_cache(summary)

    @Slot(dict)
    def _on_sweep_done(self, summary: dict):
        self._state.sweep_running = False
        self._search_panel.set_running(False)
        best = summary.get("best_pf", 0.0)
        self._progress_panel.update_progress(
            summary.get("total", 0),
            summary.get("total", 0),
            best,
            f"Sweep complete — best PF {best:.4f} | Experiment: {summary.get('experiment_id','')}",
        )

    @Slot(str)
    def _on_error(self, msg: str):
        self._state.sweep_running = False
        self._state.baseline_status = "FAIL"
        self._search_panel.set_running(False)
        self._progress_panel.update_progress(0, 1, 0.0, f"Error: {msg}")
        logger.error("ResearchLab worker error: %s", msg)
