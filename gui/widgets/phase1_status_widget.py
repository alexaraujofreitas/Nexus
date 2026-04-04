# gui/widgets/phase1_status_widget.py
"""
Phase1StatusWidget — Dashboard visibility for Phase 1 Regime-Orchestrated Architecture.

Shows:
- TransitionDetector: active transitions, signal count, last detection time
- CoverageGuarantee: current level, idle time, fallback status
- RegimeCapitalAllocator: adjustment count, last regime multiplier
- Regime distribution from scan cycles

Thread-safe: all EventBus callbacks emit Qt signals → slots run on main thread.
"""
from __future__ import annotations

import logging
import time

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QGridLayout,
)
from PySide6.QtCore import Qt, QTimer, Slot, Signal

from core.event_bus import bus, Topics

logger = logging.getLogger(__name__)

# CoverageGuarantee level names and colors
_CG_LEVEL_LABELS = {0: "Normal", 1: "INFO", 2: "EXPAND", 3: "ENRICH", 4: "NOTIFY"}
_CG_LEVEL_COLORS = {0: "#00CC77", 1: "#8899AA", 2: "#FFB300", 3: "#FF8800", 4: "#FF3355"}


class _MiniMetric(QFrame):
    """Tiny metric display: title + value + optional subtitle."""

    def __init__(self, title: str, value: str = "—", color: str = "#8899AA", parent=None):
        super().__init__(parent)
        self.setStyleSheet(
            "QFrame { background: #0D1220; border-radius: 4px; padding: 4px; }"
        )
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 4, 8, 4)
        v.setSpacing(1)

        self._title = QLabel(title)
        self._title.setStyleSheet(
            "font-size: 9px; font-weight: 700; color: #667788; letter-spacing: 0.5px;"
        )
        self._value = QLabel(value)
        self._value.setStyleSheet(f"font-size: 13px; font-weight: 700; color: {color};")
        self._sub = QLabel("")
        self._sub.setStyleSheet("font-size: 9px; color: #4A5568;")
        self._sub.setVisible(False)

        v.addWidget(self._title)
        v.addWidget(self._value)
        v.addWidget(self._sub)

    def set_value(self, text: str, color: str = "#E8EBF0"):
        self._value.setText(text)
        self._value.setStyleSheet(f"font-size: 13px; font-weight: 700; color: {color};")

    def set_sub(self, text: str):
        self._sub.setText(text)
        self._sub.setVisible(bool(text))


class Phase1StatusWidget(QFrame):
    """
    Compact widget showing Phase 1 component status on the Dashboard.

    Subscribes to TRANSITION_DETECTED, COVERAGE_GAP_DETECTED, and polls
    Phase1MetricsTracker on a 30s timer for aggregated metrics.
    """

    # Thread-safe signals — EventBus callbacks emit these, slots update UI
    _sig_transition = Signal(object)    # transition data dict
    _sig_coverage   = Signal(object)    # CG result dict
    _sig_refresh    = Signal(object)    # metrics snapshot dict

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self._enabled = False
        self._build()
        self._wire()
        self._subscribe()

        # Periodic refresh from metrics tracker (every 30 seconds)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(30_000)
        self._refresh_timer.timeout.connect(self._poll_metrics)
        self._refresh_timer.start()

        # Initial check — are any Phase 1 components enabled?
        QTimer.singleShot(2000, self._check_enabled)

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        # Title row
        title_row = QHBoxLayout()
        title = QLabel("REGIME ORCHESTRATOR")
        title.setStyleSheet(
            "font-size: 13px; font-weight: 700; color: #8899AA; letter-spacing: 1px;"
        )
        self._status_dot = QLabel("⬤ Disabled")
        self._status_dot.setStyleSheet("color: #4A5568; font-size: 12px;")
        title_row.addWidget(title)
        title_row.addStretch()
        title_row.addWidget(self._status_dot)
        layout.addLayout(title_row)

        # Metrics grid: 2 rows × 4 columns
        grid = QGridLayout()
        grid.setSpacing(6)

        # Row 1: TransitionDetector
        self._m_td_signals = _MiniMetric("TD SIGNALS", "0")
        self._m_td_active  = _MiniMetric("TD ACTIVE", "—")
        self._m_td_last    = _MiniMetric("TD LAST", "never")

        # CoverageGuarantee
        self._m_cg_level   = _MiniMetric("CG LEVEL", "0 Normal", "#00CC77")
        self._m_cg_idle    = _MiniMetric("CG IDLE", "0 cycles")
        self._m_cg_fallback = _MiniMetric("FALLBACK TRADES", "0")

        # RegimeCapitalAllocator
        self._m_rca_adj    = _MiniMetric("RCA ADJUSTMENTS", "0")
        self._m_regime     = _MiniMetric("SCANS", "0")

        grid.addWidget(self._m_td_signals,  0, 0)
        grid.addWidget(self._m_td_active,   0, 1)
        grid.addWidget(self._m_td_last,     0, 2)
        grid.addWidget(self._m_cg_level,    0, 3)
        grid.addWidget(self._m_cg_idle,     1, 0)
        grid.addWidget(self._m_cg_fallback, 1, 1)
        grid.addWidget(self._m_rca_adj,     1, 2)
        grid.addWidget(self._m_regime,      1, 3)

        layout.addLayout(grid)

        # Last event line
        self._last_event = QLabel("")
        self._last_event.setStyleSheet("font-size: 10px; color: #667788; padding-top: 2px;")
        self._last_event.setWordWrap(True)
        layout.addWidget(self._last_event)

    def _wire(self):
        self._sig_transition.connect(self._on_transition_slot)
        self._sig_coverage.connect(self._on_coverage_slot)
        self._sig_refresh.connect(self._on_refresh_slot)

    def _subscribe(self):
        bus.subscribe(Topics.TRANSITION_DETECTED, self._on_transition_event)
        bus.subscribe(Topics.COVERAGE_GAP_DETECTED, self._on_coverage_event)

    # ── EventBus callbacks (background thread) → emit signals ──

    def _on_transition_event(self, event):
        data = event.data if hasattr(event, "data") else {}
        self._sig_transition.emit(data if isinstance(data, dict) else {})

    def _on_coverage_event(self, event):
        data = event.data if hasattr(event, "data") else {}
        self._sig_coverage.emit(data if isinstance(data, dict) else {})

    # ── Qt Slots (main thread) ─────────────────────────────────

    @Slot(object)
    def _on_transition_slot(self, data: dict):
        """Handle transition signal on main thread."""
        t_type = data.get("transition_type", "?")
        direction = data.get("direction", "?")
        symbol = data.get("symbol", "?")
        conf = data.get("confidence", 0)

        self._m_td_active.set_value(
            f"{symbol} {t_type.replace('transition_', '')}",
            "#FFB300"
        )
        self._m_td_active.set_sub(f"dir={direction} conf={conf:.2f}")
        self._m_td_last.set_value("just now", "#00CC77")

        self._last_event.setText(
            f"Transition: {t_type} on {symbol} ({direction}, conf={conf:.2f})"
        )
        self._last_event.setStyleSheet("font-size: 10px; color: #FFB300; padding-top: 2px;")

    @Slot(object)
    def _on_coverage_slot(self, data: dict):
        """Handle coverage gap event on main thread."""
        level = data.get("level", 0)
        action = data.get("action", "none")
        idle = data.get("idle_cycles", 0)
        regime = data.get("dominant_regime", "?")

        label = _CG_LEVEL_LABELS.get(level, "?")
        color = _CG_LEVEL_COLORS.get(level, "#8899AA")

        self._m_cg_level.set_value(f"{level} {label}", color)
        self._m_cg_idle.set_value(f"{idle} cycles", "#FFB300" if idle > 3 else "#8899AA")
        self._m_cg_idle.set_sub(f"~{idle * 15}min in {regime}")

        remaining = data.get("fallback_trades_remaining", 0)
        self._m_cg_fallback.set_sub(f"{remaining} remaining in window")

        if level >= 2:
            self._last_event.setText(
                f"Coverage gap: L{level} {label} — {action} in {regime} ({idle * 15}min idle)"
            )
            self._last_event.setStyleSheet(f"font-size: 10px; color: {color}; padding-top: 2px;")

    @Slot(object)
    def _on_refresh_slot(self, snapshot: dict):
        """Refresh all metrics from tracker snapshot."""
        # TransitionDetector
        t_total = snapshot.get("transition_total", 0)
        self._m_td_signals.set_value(str(t_total), "#00CC77" if t_total > 0 else "#8899AA")
        self._m_td_signals.set_sub(
            f"{len(snapshot.get('transition_by_type', {}))} types"
        )

        active = snapshot.get("active_transitions", {})
        if active:
            syms = ", ".join(active.keys())
            self._m_td_active.set_value(f"{len(active)} active", "#FFB300")
            self._m_td_active.set_sub(syms[:30])
        elif t_total == 0:
            self._m_td_active.set_value("—", "#4A5568")
            self._m_td_active.set_sub("")

        last_ago = snapshot.get("last_transition_ago_min")
        if last_ago is not None:
            if last_ago < 5:
                self._m_td_last.set_value(f"{last_ago:.0f}m ago", "#00CC77")
            elif last_ago < 60:
                self._m_td_last.set_value(f"{last_ago:.0f}m ago", "#8899AA")
            else:
                self._m_td_last.set_value(f"{last_ago / 60:.1f}h ago", "#4A5568")

        # CoverageGuarantee
        cg_level = snapshot.get("cg_current_level", 0)
        label = _CG_LEVEL_LABELS.get(cg_level, "?")
        color = _CG_LEVEL_COLORS.get(cg_level, "#8899AA")
        self._m_cg_level.set_value(f"{cg_level} {label}", color)

        idle = snapshot.get("cg_idle_cycles", 0)
        self._m_cg_idle.set_value(f"{idle} cycles", "#FFB300" if idle > 3 else "#8899AA")

        fb_count = snapshot.get("cg_fallback_trade_count", 0)
        self._m_cg_fallback.set_value(str(fb_count), "#FFB300" if fb_count > 0 else "#8899AA")
        episodes = snapshot.get("cg_gap_episodes", 0)
        self._m_cg_fallback.set_sub(f"{episodes} gap episodes")

        # RegimeCapitalAllocator
        rca_count = snapshot.get("rca_adjustment_count", 0)
        self._m_rca_adj.set_value(str(rca_count), "#00CC77" if rca_count > 0 else "#8899AA")
        rca_dist = snapshot.get("rca_regime_distribution", {})
        if rca_dist:
            top = sorted(rca_dist.items(), key=lambda x: -x[1])[:2]
            self._m_rca_adj.set_sub(", ".join(f"{k}:{v}" for k, v in top))

        # Global
        scans = snapshot.get("total_scan_cycles", 0)
        self._m_regime.set_value(str(scans))
        regime_dist = snapshot.get("regime_distribution", {})
        if regime_dist:
            top = sorted(regime_dist.items(), key=lambda x: -x[1])[:3]
            self._m_regime.set_sub(", ".join(f"{k}:{v}" for k, v in top))

    # ── Periodic polling ───────────────────────────────────────

    def _poll_metrics(self):
        """Poll Phase1MetricsTracker for aggregated snapshot."""
        try:
            from core.monitoring.phase1_metrics import get_phase1_metrics
            snapshot = get_phase1_metrics().get_snapshot()
            self._sig_refresh.emit(snapshot)
        except Exception as e:
            logger.debug("Phase1StatusWidget: metrics poll error: %s", e)

    def _check_enabled(self):
        """Check if any Phase 1 components are enabled and update status dot."""
        try:
            from config.settings import settings
            td_on = bool(settings.get("transition_detector.enabled", False))
            cg_on = bool(settings.get("coverage_guarantee.enabled", False))
            rca_on = bool(settings.get("capital.regime_scaling_enabled", False))

            enabled = []
            if td_on:
                enabled.append("TD")
            if cg_on:
                enabled.append("CG")
            if rca_on:
                enabled.append("RCA")

            if enabled:
                self._enabled = True
                self._status_dot.setText(f"⬤ Active ({', '.join(enabled)})")
                self._status_dot.setStyleSheet("color: #00CC77; font-size: 12px;")
            else:
                self._status_dot.setText("⬤ All gated (config disabled)")
                self._status_dot.setStyleSheet("color: #4A5568; font-size: 12px;")
        except Exception:
            self._status_dot.setText("⬤ Unknown")
            self._status_dot.setStyleSheet("color: #4A5568; font-size: 12px;")

        # Do initial poll
        self._poll_metrics()
