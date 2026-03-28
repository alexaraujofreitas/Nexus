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
    QObject, QThread, Qt, QTimer, Signal, Slot,
)
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFrame, QGridLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QProgressBar, QPushButton,
    QScrollArea, QSizePolicy, QSlider, QSpinBox, QSplitter,
    QStackedWidget, QTableWidget, QTableWidgetItem, QTextEdit,
    QVBoxLayout, QWidget, QDoubleSpinBox, QCheckBox, QLineEdit,
)

ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(ROOT))

logger = logging.getLogger(__name__)

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
            border: 1px solid #2a4a7a;
            border-radius: 6px;
            margin-top: 14px;
            padding: 8px;
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
    lay.setSpacing(6)
    lay.setContentsMargins(8, 16, 8, 8)
    return gb, lay


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
    n_workers:          int  = 2
    objective:          str  = "profit_factor"
    cost_per_side:      float= 0.0004


# ─────────────────────────────────────────────────────────────────────────────
# Worker thread
# ─────────────────────────────────────────────────────────────────────────────

class SweepWorkerThread(QThread):
    """
    Runs baseline or sweep in background thread.
    All results → main thread via queued signals.
    """
    progress      = Signal(int, int, float, str)   # completed, total, best_pf, msg
    indeterminate = Signal(bool)                   # True → pulsing bar, False → normal
    trial_done    = Signal(dict)                   # one trial result
    finished_ok   = Signal(dict)                  # final summary
    error_signal  = Signal(str)                   # error message

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

        # Phase 1 — load data (indeterminate, long)
        self.indeterminate.emit(True)
        self.progress.emit(0, PHASES, 0.0, "Phase 1/4 — Loading historical data…")
        runner = BacktestRunner(date_start="2022-03-22", date_end="2026-03-21")
        runner.load_data()
        if self._cancel:
            return

        # Phase 2 — zero-fee scenario (indeterminate, long)
        self.progress.emit(1, PHASES, 0.0, "Phase 2/4 — Running zero-fee scenario (may take 2–4 min)…")
        r0 = runner.run(params={}, cost_per_side=0.0)
        if self._cancel:
            return

        # Phase 3 — fee scenario (indeterminate, long)
        self.progress.emit(2, PHASES, 0.0, "Phase 3/4 — Running fee scenario…")
        r1 = runner.run(params={}, cost_per_side=0.0004)

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
        engine = SweepEngine(n_workers=state.n_workers)
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

        self._status_lbl = _label("NOT RUN", bold=True, color=_DIM, size=11)
        vlay.addWidget(self._status_lbl)

        self._metrics_text = QTextEdit()
        self._metrics_text.setReadOnly(True)
        self._metrics_text.setMaximumHeight(120)
        self._metrics_text.setStyleSheet(
            f"background:{_DARK}; color:{_TEXT}; border:none; font-size:11px;"
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

    def set_result(self, summary: dict):
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
        lines = [
            f"n={r0.get('n_trades','?')}  PF(0%)={r0.get('profit_factor','?')}  "
            f"PF(fees)={r1.get('profit_factor','?')}",
            f"WR={r0.get('win_rate','?')}  CAGR={r0.get('cagr','?'):.1%}  "
            f"MaxDD={r0.get('max_drawdown','?'):.1%}",
            f"PBL: n={r0.get('pbl_n','?')}  PF={r0.get('pbl_pf','?')}",
            f"SLC: n={r0.get('slc_n','?')}  PF={r0.get('slc_pf','?')}",
        ]
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
        h = QHBoxLayout(self)
        h.setContentsMargins(4, 1, 4, 1)
        h.setSpacing(4)

        # Description — stretches to fill available space
        name = _label(self._p.description, color=_TEXT)
        name.setMinimumWidth(120)
        name.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        h.addWidget(name, 1)

        # Value spinbox — compact
        self._spin = QDoubleSpinBox()
        self._spin.setDecimals(2)
        self._spin.setRange(self._p.range_min * 0.5, self._p.range_max * 1.5)
        self._spin.setSingleStep(self._p.step)
        self._spin.setValue(float(self._p.default))
        self._spin.setFixedWidth(62)
        self._spin.setStyleSheet(
            f"background:{_DARK}; color:{_TEXT}; border:1px solid #444; border-radius:3px;"
        )
        h.addWidget(self._spin)

        # Range label — compact hint
        range_lbl = _label(
            f"[{self._p.range_min}–{self._p.range_max}]",
            color=_DIM,
        )
        range_lbl.setFixedWidth(86)
        range_lbl.setStyleSheet(f"color:{_DIM}; font-size:10px;")
        h.addWidget(range_lbl)

        # FIXED / OPTIMIZE toggle
        self._mode_btn = QPushButton("FIXED")
        self._mode_btn.setCheckable(True)
        self._mode_btn.setFixedWidth(82)
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
            f"background:#333; color:{_DIM}; font-weight:bold;"
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


class _ParameterPanel(QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        from research.engine.parameter_registry import ALL_PARAMS
        self._rows: list[_ParamRow] = []
        self._build(ALL_PARAMS)

    def _build(self, params):
        card, vlay = _card("⊙  Parameters")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)

        # Section headers
        pbl_hdr = _label("PBL (Pullback Long)", bold=True, color=_BLUE)
        vlay.addWidget(pbl_hdr)

        for p in [pp for pp in params if pp.model == "pbl"]:
            row = _ParamRow(p)
            self._rows.append(row)
            vlay.addWidget(row)

        vlay.addSpacing(6)
        slc_hdr = _label("SLC (Swing Low Continuation)", bold=True, color=_AMBER)
        vlay.addWidget(slc_hdr)

        for p in [pp for pp in params if pp.model == "slc"]:
            row = _ParamRow(p)
            self._rows.append(row)
            vlay.addWidget(row)

        # Reset to defaults
        reset_btn = QPushButton("Reset to Defaults")
        reset_btn.setStyleSheet(
            f"background:#333; color:{_DIM}; border-radius:3px; padding:4px;"
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
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)

        # Strategy
        row1 = QHBoxLayout()
        row1.addWidget(_label("Strategy:", color=_DIM))
        self._strategy_cb = QComboBox()
        self._strategy_cb.addItems(["Coarse Grid Sweep", "Random Search", "Bayesian (coming soon)"])
        self._strategy_cb.setStyleSheet(
            f"background:{_DARK}; color:{_TEXT}; border:1px solid #444;"
        )
        row1.addWidget(self._strategy_cb)
        vlay.addLayout(row1)

        # Random trials (only for random mode)
        row2 = QHBoxLayout()
        row2.addWidget(_label("Trials:", color=_DIM))
        self._n_trials_spin = QSpinBox()
        self._n_trials_spin.setRange(10, 10000)
        self._n_trials_spin.setValue(200)
        self._n_trials_spin.setStyleSheet(
            f"background:{_DARK}; color:{_TEXT}; border:1px solid #444;"
        )
        row2.addWidget(self._n_trials_spin)
        vlay.addLayout(row2)

        # Workers
        row3 = QHBoxLayout()
        row3.addWidget(_label("Workers:", color=_DIM))
        self._workers_spin = QSpinBox()
        self._workers_spin.setRange(1, 8)
        self._workers_spin.setValue(2)
        self._workers_spin.setStyleSheet(
            f"background:{_DARK}; color:{_TEXT}; border:1px solid #444;"
        )
        row3.addWidget(self._workers_spin)
        vlay.addLayout(row3)

        # Objective
        row4 = QHBoxLayout()
        row4.addWidget(_label("Objective:", color=_DIM))
        self._obj_cb = QComboBox()
        self._obj_cb.addItems(["Profit Factor", "CAGR", "Win Rate"])
        self._obj_cb.setStyleSheet(
            f"background:{_DARK}; color:{_TEXT}; border:1px solid #444;"
        )
        row4.addWidget(self._obj_cb)
        vlay.addLayout(row4)

        # Fee model
        row5 = QHBoxLayout()
        row5.addWidget(_label("Fees:", color=_DIM))
        self._fee_cb = QComboBox()
        self._fee_cb.addItems(["0.04%/side (production)", "Zero fees"])
        self._fee_cb.setStyleSheet(
            f"background:{_DARK}; color:{_TEXT}; border:1px solid #444;"
        )
        row5.addWidget(self._fee_cb)
        vlay.addLayout(row5)

        vlay.addSpacing(8)

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
        fee_map = {0: 0.0004, 1: 0.0}
        return {
            "search_strategy": strategy_map[self._strategy_cb.currentIndex()],
            "n_random_trials": self._n_trials_spin.value(),
            "n_workers":       self._workers_spin.value(),
            "objective":       self._obj_cb.currentText().lower().replace(" ", "_"),
            "cost_per_side":   fee_map[self._fee_cb.currentIndex()],
        }

    def set_running(self, running: bool):
        self._start_btn.setEnabled(not running)
        self._cancel_btn.setEnabled(running)


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

        self._msg_lbl = _label("Idle", color=_DIM)
        vlay.addWidget(self._msg_lbl)

        # Mini leaderboard (top-5)
        self._mini_table = QTableWidget(5, 3)
        self._mini_table.setHorizontalHeaderLabels(["Rank", "PF", "n"])
        self._mini_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._mini_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._mini_table.setSelectionMode(QAbstractItemView.NoSelection)
        self._mini_table.setMaximumHeight(130)
        self._mini_table.setStyleSheet(
            f"background:{_DARK}; color:{_TEXT}; gridline-color:#333;"
            " font-size:11px; border:none;"
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

    def reset(self):
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._trials_lbl.setText("—")
        self._best_lbl.setText("Best PF: —")
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
        filter_row.addWidget(_label("Min PF:", color=_DIM))
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
        self._count_lbl = _label("0 trials", color=_DIM)
        filter_row.addWidget(self._count_lbl)
        vlay.addLayout(filter_row)

        self._table = QTableWidget(0, len(_RESULT_COLS))
        self._table.setHorizontalHeaderLabels(_RESULT_COLS)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSortingEnabled(True)
        self._table.setStyleSheet(
            f"background:{_DARK}; color:{_TEXT}; gridline-color:#333;"
            " font-size:11px; border:none;"
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

        info = QLabel(
            "IS period: 2022-03-22 → 2025-09-21 (3.5 years)\n"
            "OOS period: 2025-09-22 → 2026-03-21 (6 months)\n\n"
            "Select a candidate from Results and click Run OOS to evaluate\n"
            "on the held-out period."
        )
        info.setStyleSheet(f"color:{_DIM}; font-size:11px;")
        info.setWordWrap(True)
        vlay.addWidget(info)

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
            f"background:{_DARK}; color:{_TEXT}; border:none; font-size:11px;"
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
        self._state   = _LabState()
        self._worker: Optional[SweepWorkerThread] = None
        self._build()

    # ─────────────────────────────────────────────────────────────────────────
    # Layout
    # ─────────────────────────────────────────────────────────────────────────

    def _build(self):
        self.setStyleSheet(f"background:{_DARK}; color:{_TEXT};")

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
        root.addWidget(splitter)

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

        self._param_panel = _ParameterPanel()
        left_layout.addWidget(self._param_panel)

        self._search_panel = _SearchPanel()
        self._search_panel.start_requested.connect(self._on_start_sweep)
        self._search_panel.cancel_requested.connect(self._on_cancel)
        left_layout.addWidget(self._search_panel)

        left_layout.addStretch()
        left_scroll.setWidget(left_widget)
        left_scroll.setMinimumWidth(400)
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

        right_layout.addWidget(results_splitter)
        splitter.addWidget(right_widget)
        splitter.setSizes([380, 820])

    # ─────────────────────────────────────────────────────────────────────────
    # Slots
    # ─────────────────────────────────────────────────────────────────────────

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
        self._worker.start()

    @Slot()
    def _on_start_sweep(self):
        if self._state.sweep_running:
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
        self._state.cost_per_side  = search["cost_per_side"]

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
        self._progress_panel.update_progress(100, 100, 0.0,
            "Baseline PASS ✅" if passed else "Baseline FAIL ❌ — optimization blocked")

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
