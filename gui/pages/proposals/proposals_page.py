# ============================================================
# NEXUS TRADER — Tuning Proposals Review Page (Phase 3)
#
# Operational review panel for the adaptive-learning pipeline.
# Lists pending proposals, shows trigger evidence, allows
# manual approve / reject, and shows applied-change history.
# ============================================================
from __future__ import annotations

import json
import logging
from typing import Optional

from PySide6.QtCore import Qt, Signal, QThread
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QSplitter,
    QTextEdit, QComboBox, QFrame, QMessageBox, QAbstractItemView,
    QSizePolicy,
)

logger = logging.getLogger(__name__)


# ── Colour palette (matches system theme) ─────────────────────
_CLR_BG     = "#1A1E2E"
_CLR_PANEL  = "#232840"
_CLR_BORDER = "#2E3550"
_CLR_TEXT   = "#E0E6F0"
_CLR_DIM    = "#8899AA"
_CLR_GREEN  = "#00C878"
_CLR_YELLOW = "#FFB300"
_CLR_RED    = "#FF3355"
_CLR_BLUE   = "#4499FF"
_CLR_PURPLE = "#9966FF"

_STATUS_COLOUR = {
    "pending":        _CLR_YELLOW,
    "approved":       _CLR_GREEN,
    "rejected":       _CLR_RED,
    "applied":        _CLR_BLUE,
    "APPROVE":        _CLR_GREEN,
    "REJECT":         _CLR_RED,
    "MANUAL_REVIEW":  _CLR_YELLOW,
    "ERROR":          _CLR_RED,
}

_SEVERITY_COLOUR = {
    "critical": _CLR_RED,
    "major":    _CLR_YELLOW,
    "minor":    _CLR_DIM,
}


class BacktestWorker(QThread):
    """Run a proposal backtest in a background thread."""
    finished  = Signal(dict)   # run_record
    progress  = Signal(str)    # status message

    def __init__(self, proposal: dict, symbol: str = "BTCUSDT",
                 timeframe: str = "1h"):
        super().__init__()
        self._proposal  = proposal
        self._symbol    = symbol
        self._timeframe = timeframe

    def run(self) -> None:
        self.progress.emit(f"Running backtest for {self._proposal.get('proposal_id')}…")
        try:
            from core.analysis.backtest_runner import run_proposal_backtest
            record = run_proposal_backtest(
                self._proposal,
                symbol=self._symbol,
                timeframe=self._timeframe,
            )
        except Exception as e:
            record = {
                "proposal_id": self._proposal.get("proposal_id"),
                "gating_result": "ERROR",
                "error": str(e),
            }
        self.finished.emit(record)


class ProposalsPage(QWidget):
    """Tuning Proposals review panel."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._proposals: list[dict] = []
        self._applied:   list[dict] = []
        self._selected_proposal: Optional[dict] = None
        self._backtest_worker: Optional[BacktestWorker] = None
        self._setup_ui()
        self._refresh()

    # ── UI construction ────────────────────────────────────────

    def _setup_ui(self) -> None:
        self.setObjectName("proposals_page")
        self.setStyleSheet(f"""
            QWidget#proposals_page {{ background: {_CLR_BG}; color: {_CLR_TEXT}; }}
            QLabel {{ color: {_CLR_TEXT}; }}
            QPushButton {{
                background: {_CLR_PANEL}; color: {_CLR_TEXT};
                border: 1px solid {_CLR_BORDER}; border-radius: 4px;
                padding: 5px 12px; font-size: 12px;
            }}
            QPushButton:hover {{ background: #2E3550; }}
            QPushButton:disabled {{ color: {_CLR_DIM}; }}
            QTableWidget {{
                background: {_CLR_PANEL}; color: {_CLR_TEXT};
                gridline-color: {_CLR_BORDER};
                border: 1px solid {_CLR_BORDER};
            }}
            QHeaderView::section {{
                background: {_CLR_BG}; color: {_CLR_DIM};
                border: 1px solid {_CLR_BORDER}; padding: 4px; font-size: 11px;
            }}
            QTextEdit {{
                background: {_CLR_PANEL}; color: {_CLR_TEXT};
                border: 1px solid {_CLR_BORDER}; font-family: monospace; font-size: 12px;
            }}
            QComboBox {{
                background: {_CLR_PANEL}; color: {_CLR_TEXT};
                border: 1px solid {_CLR_BORDER}; padding: 3px 8px; border-radius: 4px;
            }}
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        # ── Header ─────────────────────────────────────────────
        hdr = QHBoxLayout()
        title = QLabel("Tuning Proposals")
        title.setStyleSheet(f"font-size:18px; font-weight:bold; color:{_CLR_TEXT};")
        subtitle = QLabel("Adaptive learning proposals — review, backtest, approve or reject")
        subtitle.setStyleSheet(f"font-size:12px; color:{_CLR_DIM};")
        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet(f"font-size:11px; color:{_CLR_DIM};")
        hdr.addWidget(title)
        hdr.addSpacing(16)
        hdr.addWidget(subtitle)
        hdr.addStretch()
        hdr.addWidget(self._status_lbl)

        btn_refresh = QPushButton("⟳ Refresh")
        btn_refresh.setFixedWidth(90)
        btn_refresh.clicked.connect(self._refresh)
        hdr.addWidget(btn_refresh)
        root.addLayout(hdr)

        # ── Summary strip ──────────────────────────────────────
        strip = QHBoxLayout()
        self._lbl_pending  = self._stat_chip("Pending",  "0", _CLR_YELLOW)
        self._lbl_approved = self._stat_chip("Approved", "0", _CLR_GREEN)
        self._lbl_rejected = self._stat_chip("Rejected", "0", _CLR_RED)
        self._lbl_applied  = self._stat_chip("Applied",  "0", _CLR_BLUE)
        for w in (self._lbl_pending, self._lbl_approved,
                  self._lbl_rejected, self._lbl_applied):
            strip.addWidget(w)
        strip.addStretch()
        root.addLayout(strip)

        # ── Splitter: proposals table (top) + detail panel (bottom) ──
        splitter = QSplitter(Qt.Vertical)

        # ── Proposals table ────────────────────────────────────
        self._table = QTableWidget()
        self._table.setColumnCount(7)
        self._table.setHorizontalHeaderLabels([
            "Category", "Parameter", "Direction",
            "Confidence", "Severity", "Auto-Tune", "Status",
        ])
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setStyleSheet(
            f"alternate-background-color: {_CLR_BG}; background: {_CLR_PANEL};"
        )
        self._table.itemSelectionChanged.connect(self._on_row_selected)
        splitter.addWidget(self._table)

        # ── Detail panel ───────────────────────────────────────
        detail_frame = QFrame()
        detail_frame.setStyleSheet(
            f"QFrame {{ background:{_CLR_PANEL}; border-top:1px solid {_CLR_BORDER}; }}"
        )
        detail_layout = QVBoxLayout(detail_frame)
        detail_layout.setContentsMargins(12, 8, 12, 8)
        detail_layout.setSpacing(8)

        # Detail header row
        detail_hdr = QHBoxLayout()
        detail_title = QLabel("Proposal Detail")
        detail_title.setStyleSheet(
            f"font-size:14px; font-weight:bold; color:{_CLR_TEXT};"
        )

        # Action buttons
        self._btn_approve = QPushButton("✓ Approve")
        self._btn_approve.setStyleSheet(
            f"background:{_CLR_GREEN}; color:#000; font-weight:bold;"
        )
        self._btn_approve.setFixedWidth(110)
        self._btn_approve.clicked.connect(self._on_approve)
        self._btn_approve.setEnabled(False)

        self._btn_reject = QPushButton("✗ Reject")
        self._btn_reject.setStyleSheet(
            f"background:{_CLR_RED}; color:#FFF; font-weight:bold;"
        )
        self._btn_reject.setFixedWidth(110)
        self._btn_reject.clicked.connect(self._on_reject)
        self._btn_reject.setEnabled(False)

        # Backtest symbol/TF selectors
        sym_lbl = QLabel("Symbol:")
        sym_lbl.setStyleSheet(f"color:{_CLR_DIM};")
        self._sym_combo = QComboBox()
        self._sym_combo.addItems(["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT"])
        self._sym_combo.setFixedWidth(100)

        tf_lbl = QLabel("TF:")
        tf_lbl.setStyleSheet(f"color:{_CLR_DIM};")
        self._tf_combo = QComboBox()
        self._tf_combo.addItems(["1h", "4h", "15m"])
        self._tf_combo.setFixedWidth(70)

        self._btn_backtest = QPushButton("▶ Run Backtest")
        self._btn_backtest.setStyleSheet(
            f"background:{_CLR_BLUE}; color:#FFF; font-weight:bold;"
        )
        self._btn_backtest.setFixedWidth(130)
        self._btn_backtest.clicked.connect(self._on_run_backtest)
        self._btn_backtest.setEnabled(False)

        detail_hdr.addWidget(detail_title)
        detail_hdr.addStretch()
        detail_hdr.addWidget(sym_lbl)
        detail_hdr.addWidget(self._sym_combo)
        detail_hdr.addWidget(tf_lbl)
        detail_hdr.addWidget(self._tf_combo)
        detail_hdr.addWidget(self._btn_backtest)
        detail_hdr.addSpacing(12)
        detail_hdr.addWidget(self._btn_approve)
        detail_hdr.addWidget(self._btn_reject)
        detail_layout.addLayout(detail_hdr)

        self._detail_text = QTextEdit()
        self._detail_text.setReadOnly(True)
        self._detail_text.setMinimumHeight(180)
        detail_layout.addWidget(self._detail_text)

        splitter.addWidget(detail_frame)
        splitter.setSizes([350, 280])
        root.addWidget(splitter, 1)

        # ── Applied history tab ────────────────────────────────
        applied_frame = QFrame()
        applied_frame.setStyleSheet(
            f"QFrame {{ background:{_CLR_PANEL}; border-top:1px solid {_CLR_BORDER}; }}"
        )
        applied_layout = QVBoxLayout(applied_frame)
        applied_layout.setContentsMargins(12, 6, 12, 6)

        applied_hdr = QLabel("Applied Change History")
        applied_hdr.setStyleSheet(
            f"font-size:13px; font-weight:bold; color:{_CLR_TEXT};"
        )
        applied_layout.addWidget(applied_hdr)

        self._applied_table = QTableWidget()
        self._applied_table.setColumnCount(6)
        self._applied_table.setHorizontalHeaderLabels([
            "Proposal ID", "Parameter", "Direction",
            "PF Delta%", "Applied By", "Applied At",
        ])
        self._applied_table.horizontalHeader().setStretchLastSection(True)
        self._applied_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self._applied_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._applied_table.setAlternatingRowColors(True)
        self._applied_table.setMaximumHeight(130)
        self._applied_table.setStyleSheet(
            f"alternate-background-color: {_CLR_BG}; background: {_CLR_PANEL};"
        )
        applied_layout.addWidget(self._applied_table)
        root.addWidget(applied_frame)

    def _stat_chip(self, label: str, value: str, colour: str) -> QLabel:
        lbl = QLabel(f"  {label}: {value}  ")
        lbl.setStyleSheet(
            f"background:{_CLR_PANEL}; color:{colour}; border:1px solid {colour};"
            f"border-radius:3px; padding:3px 8px; font-size:11px; font-weight:bold;"
        )
        lbl.setFixedHeight(24)
        return lbl

    # ── Data loading ───────────────────────────────────────────

    def _refresh(self) -> None:
        """Reload proposals from DB and repaint."""
        try:
            from core.analysis.tuning_proposal_generator import load_pending_proposals
            from core.analysis.backtest_gating import load_pending_proposals as _lp2
            try:
                self._proposals = _lp2()
            except Exception:
                self._proposals = load_pending_proposals()
        except Exception as e:
            logger.warning("ProposalsPage: could not load proposals: %s", e)
            self._proposals = []

        try:
            from core.analysis.backtest_gating import load_applied_changes
            self._applied = load_applied_changes()
        except Exception:
            self._applied = []

        self._populate_table()
        self._populate_applied_table()
        self._update_summary_strip()
        self._status_lbl.setText(
            f"Last refreshed: {__import__('datetime').datetime.now().strftime('%H:%M:%S')}"
        )

    def _populate_table(self) -> None:
        self._table.setRowCount(0)
        for p in self._proposals:
            row = self._table.rowCount()
            self._table.insertRow(row)

            status   = str(p.get("status", "pending"))
            severity = p.get("trigger_evidence", {}).get("severity") or "major"
            auto_tune = "✓" if p.get("auto_tune_eligible") else "✗"
            conf = f"{float(p.get('confidence', 0)):.0%}"
            colour = _STATUS_COLOUR.get(status, _CLR_TEXT)
            sev_colour = _SEVERITY_COLOUR.get(severity.lower(), _CLR_DIM)

            cells = [
                (p.get("root_cause_category", ""), _CLR_TEXT),
                (p.get("tuning_parameter", ""),     _CLR_DIM),
                (p.get("tuning_direction", ""),      _CLR_DIM),
                (conf,                               _CLR_BLUE),
                (severity.upper(),                   sev_colour),
                (auto_tune,                          _CLR_GREEN if auto_tune == "✓" else _CLR_RED),
                (status.upper(),                     colour),
            ]
            for col, (text, clr) in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setForeground(QColor(clr))
                item.setTextAlignment(Qt.AlignVCenter | Qt.AlignLeft)
                self._table.setItem(row, col, item)

        self._table.resizeColumnsToContents()

    def _populate_applied_table(self) -> None:
        self._applied_table.setRowCount(0)
        for ch in self._applied:
            row = self._applied_table.rowCount()
            self._applied_table.insertRow(row)
            pf_delta = ch.get("backtest_delta_pf_pct")
            pf_str = f"+{pf_delta:.1f}%" if pf_delta and pf_delta > 0 else (
                f"{pf_delta:.1f}%" if pf_delta else "—")
            cells = [
                ch.get("proposal_id", ""),
                ch.get("tuning_parameter", ""),
                ch.get("tuning_direction", ""),
                pf_str,
                ch.get("applied_by", ""),
                str(ch.get("applied_at", ""))[:16],
            ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setForeground(QColor(_CLR_TEXT))
                self._applied_table.setItem(row, col, item)

    def _update_summary_strip(self) -> None:
        pending  = sum(1 for p in self._proposals if p.get("status") == "pending")
        approved = sum(1 for p in self._proposals if p.get("status") == "approved")
        rejected = sum(1 for p in self._proposals if p.get("status") == "rejected")
        applied  = len(self._applied)
        self._lbl_pending.setText(f"  Pending: {pending}  ")
        self._lbl_approved.setText(f"  Approved: {approved}  ")
        self._lbl_rejected.setText(f"  Rejected: {rejected}  ")
        self._lbl_applied.setText(f"  Applied: {applied}  ")

    # ── Row selection ──────────────────────────────────────────

    def _on_row_selected(self) -> None:
        rows = self._table.selectedItems()
        if not rows:
            self._selected_proposal = None
            self._set_action_buttons(False)
            return
        row_idx = self._table.currentRow()
        if row_idx < 0 or row_idx >= len(self._proposals):
            return
        self._selected_proposal = self._proposals[row_idx]
        self._render_detail(self._selected_proposal)
        is_pending = self._selected_proposal.get("status") == "pending"
        self._set_action_buttons(is_pending)

    def _set_action_buttons(self, enabled: bool) -> None:
        self._btn_approve.setEnabled(enabled)
        self._btn_reject.setEnabled(enabled)
        self._btn_backtest.setEnabled(enabled)

    def _render_detail(self, p: dict) -> None:
        ev = p.get("trigger_evidence") or {}
        bt = p.get("backtest_result") or {}

        lines = [
            f"Proposal ID:       {p.get('proposal_id', '—')}",
            f"Root Cause:        {p.get('root_cause_category', '—')}",
            f"Rec ID:            {p.get('rec_id', '—')}",
            f"Status:            {p.get('status', 'pending').upper()}",
            "",
            "── TRIGGER EVIDENCE ─────────────────────────────────",
            f"  Trade count:     {ev.get('count', ev.get('occurrence_count', '—'))}",
            f"  Occurrence:      {ev.get('pct', ev.get('occurrence_pct', 0)):.1f}%  (threshold ≥ 20%)",
            f"  Severity:        {ev.get('severity', '—').upper()}",
            f"  Avg PnL:         ${ev.get('avg_pnl_usdt', 0):.2f}",
            f"  Avg score:       {ev.get('avg_overall_score', 0):.1f}/100",
            "",
            "── PROPOSED CHANGE ──────────────────────────────────",
            f"  Parameter:       {p.get('tuning_parameter', '—')}",
            f"  Direction:       {p.get('tuning_direction', '—')}",
            f"  Description:     {p.get('proposed_change_description', '—')}",
            f"  Expected benefit:{p.get('expected_benefit', '—')}",
            "",
            "── CONFIDENCE & RISK ────────────────────────────────",
            f"  Confidence:      {float(p.get('confidence', 0)):.0%}",
            f"  Risk level:      {p.get('risk_level', '—')}",
            f"  Auto-tune safe:  {'YES' if p.get('auto_tune_eligible') else 'NO — manual approval required'}",
            f"  Requires manual: {'YES' if p.get('requires_manual_approval') else 'no'}",
        ]

        if bt:
            decision = bt.get("decision") or p.get("backtest_result", {}).get("gating_result", "")
            lines += [
                "",
                "── BACKTEST RESULT ──────────────────────────────────",
                f"  Decision:        {decision}",
                f"  PF delta:        {bt.get('pf_delta_pct', 0):.2f}%",
                f"  WR delta:        {bt.get('wr_delta_pp', 0):.2f}pp",
                f"  Auto-promotable: {'YES' if bt.get('auto_promotable') else 'no'}",
                f"  Ran at:          {str(bt.get('ran_at', '—'))[:19]}",
            ]
        else:
            lines += ["", "── BACKTEST RESULT ──────────────────────────────────",
                      "  Not yet run. Use ▶ Run Backtest to evaluate."]

        self._detail_text.setPlainText("\n".join(lines))

    # ── Actions ────────────────────────────────────────────────

    def _on_approve(self) -> None:
        if not self._selected_proposal:
            return
        pid = self._selected_proposal.get("proposal_id", "")
        reply = QMessageBox.question(
            self, "Approve Proposal",
            f"Approve proposal '{pid}'?\n\n"
            "This will mark it as approved. Apply it manually via settings.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self._update_proposal_status(pid, "approved")

    def _on_reject(self) -> None:
        if not self._selected_proposal:
            return
        pid = self._selected_proposal.get("proposal_id", "")
        reply = QMessageBox.question(
            self, "Reject Proposal",
            f"Reject proposal '{pid}'?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self._update_proposal_status(pid, "rejected")

    def _update_proposal_status(self, proposal_id: str, status: str) -> None:
        try:
            from core.analysis.backtest_gating import update_proposal_after_evaluation
            update_proposal_after_evaluation(proposal_id, {"decision": status.upper()}, {})
            logger.info("ProposalsPage: set %s → %s", proposal_id, status)
        except Exception as e:
            logger.warning("ProposalsPage: could not update proposal status: %s", e)
        self._refresh()

    def _on_run_backtest(self) -> None:
        if not self._selected_proposal:
            return
        if self._backtest_worker and self._backtest_worker.isRunning():
            return
        symbol = self._sym_combo.currentText()
        tf     = self._tf_combo.currentText()
        self._btn_backtest.setEnabled(False)
        self._btn_backtest.setText("⏳ Running…")
        self._status_lbl.setText(f"Running backtest for {symbol}/{tf}…")
        self._backtest_worker = BacktestWorker(
            self._selected_proposal, symbol=symbol, timeframe=tf
        )
        self._backtest_worker.finished.connect(self._on_backtest_done)
        self._backtest_worker.progress.connect(
            lambda msg: self._status_lbl.setText(msg)
        )
        self._backtest_worker.start()

    def _on_backtest_done(self, record: dict) -> None:
        self._btn_backtest.setEnabled(True)
        self._btn_backtest.setText("▶ Run Backtest")
        decision = record.get("gating_result", "?")
        colour = _STATUS_COLOUR.get(decision, _CLR_TEXT)
        self._status_lbl.setText(
            f"Backtest done: {decision}  |  "
            f"PF delta: {record.get('pf_delta_pct', 0):.2f}%"
        )
        self._refresh()
