# ============================================================
# NEXUS TRADER — AI Intelligence Dashboard  (Phase 12)
#
# Full-page intelligence view showing:
#   - WHY the system is bullish or bearish (signal breakdown)
#   - OrchestratorEngine meta-signal with contributor weights
#   - All 8 agent statuses with live signals
#   - Current regime + HMM probability distribution
#   - Macro veto state and active risk conditions
#   - Signal contribution chart (who is driving the view)
# ============================================================
from __future__ import annotations

import logging
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QPushButton, QScrollArea, QGroupBox, QGridLayout,
    QProgressBar, QSizePolicy,
)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont

from gui.main_window import PageHeader
from core.event_bus import bus, Topics

logger = logging.getLogger(__name__)

# ── Palette constants ─────────────────────────────────────────
_GREEN  = "#00FF88"
_RED    = "#FF3355"
_ORANGE = "#FF6B00"
_YELLOW = "#F6AD55"
_BLUE   = "#4299E1"
_GRAY   = "#4A5568"
_LIGHT  = "#C8D0E0"
_BG     = "#0D1B2A"
_PANEL  = "#0F1923"

# Agent weights (mirror orchestrator weights for display).
# Disabled agents are filtered out at page construction time so they never
# appear in the Signal Contributors grid or the narrative builder.
_AGENT_WEIGHTS_ALL = {
    "funding_rate":    0.25,
    "order_book":      0.22,
    "options_flow":    0.18,
    "macro":           0.17,
    "social_sentiment":0.08,
    "news":            0.05,
    "geopolitical":    0.03,
    "sector_rotation": 0.02,
}

# Config gate map: agent name → (settings_key, default_if_missing)
# Agents not in this map are always shown (no disable gate in coordinator).
_AGENT_GATE: dict[str, tuple[str, bool]] = {
    "order_book":       ("agents.orderbook_enabled",        True),
    "options_flow":     ("agents.options_enabled",          True),
    "social_sentiment": ("agents.social_sentiment_enabled", True),
    "sector_rotation":  ("agents.sector_rotation_enabled",  True),
    "funding_rate":     ("agents.funding_enabled",          True),
}

def _build_agent_weights() -> dict[str, float]:
    """Return only the weights for agents that are enabled in config."""
    try:
        from config.settings import settings as _s
    except Exception:
        return _AGENT_WEIGHTS_ALL

    result = {}
    for name, weight in _AGENT_WEIGHTS_ALL.items():
        if name in _AGENT_GATE:
            key, default = _AGENT_GATE[name]
            if not _s.get(key, default):
                continue
        result[name] = weight
    return result

_AGENT_WEIGHTS = _build_agent_weights()

_REGIME_EMOJIS = {
    "bull_trend":             "🐂  Bull Trend",
    "bear_trend":             "🐻  Bear Trend",
    "ranging":                "↔  Ranging",
    "volatility_expansion":   "⚡  Vol Expansion",
    "volatility_compression": "🗜  Vol Squeeze",
    "uncertain":              "❓  Uncertain",
    "accumulation":           "🟢  Accumulation",
    "distribution":           "🔴  Distribution",
}


# ── Compact status card ───────────────────────────────────────
class _AgentCard(QFrame):
    """Compact card showing one agent's contribution to the meta-view."""

    def __init__(self, agent_name: str, weight: float, parent=None):
        super().__init__(parent)
        self._name   = agent_name
        self._weight = weight
        self.setObjectName("AgentCard")
        self.setFrameShape(QFrame.StyledPanel)
        self.setFixedHeight(72)
        self.setMinimumWidth(220)
        self._build()

    def _build(self) -> None:
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 8, 12, 8)
        v.setSpacing(4)

        # Header row
        h = QHBoxLayout()
        h.setSpacing(8)
        self._name_lbl = QLabel(self._name.replace("_", " ").title())
        self._name_lbl.setStyleSheet(f"color:{_LIGHT}; font-weight:600; font-size:13px;")
        h.addWidget(self._name_lbl)
        h.addStretch()
        wt_lbl = QLabel(f"w={self._weight:.2f}")
        wt_lbl.setStyleSheet(f"color:{_GRAY}; font-size:13px;")
        h.addWidget(wt_lbl)
        self._status_lbl = QLabel("●")
        self._status_lbl.setStyleSheet(f"color:{_GRAY};")
        h.addWidget(self._status_lbl)
        v.addLayout(h)

        # Signal bar row
        h2 = QHBoxLayout()
        h2.setSpacing(8)
        self._signal_lbl = QLabel("—")
        self._signal_lbl.setStyleSheet(f"color:{_GRAY}; font-size:13px; font-weight:700;")
        self._signal_lbl.setFixedWidth(60)
        h2.addWidget(self._signal_lbl)

        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setValue(50)
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(6)
        h2.addWidget(self._bar, 1)

        self._conf_lbl = QLabel("conf:—")
        self._conf_lbl.setStyleSheet(f"color:{_GRAY}; font-size:13px;")
        self._conf_lbl.setFixedWidth(55)
        h2.addWidget(self._conf_lbl)
        v.addLayout(h2)

    def refresh(self, status: dict) -> None:
        running   = status.get("running", False)
        stale     = status.get("stale", True)
        signal    = float(status.get("signal", 0.0))
        conf      = float(status.get("confidence", 0.0))

        # Status dot
        if not running:
            dot_color = _GRAY
        elif stale:
            dot_color = _YELLOW
        else:
            dot_color = _GREEN
        self._status_lbl.setStyleSheet(f"color:{dot_color};")

        # Signal label and color
        if conf > 0:
            if signal > 0.05:
                color = _GREEN
                arrow = f"▲ {signal:+.3f}"
            elif signal < -0.05:
                color = _RED
                arrow = f"▼ {signal:+.3f}"
            else:
                color = _GRAY
                arrow = f"— {signal:+.3f}"
        else:
            color = _GRAY
            arrow = "—"

        self._signal_lbl.setText(arrow)
        self._signal_lbl.setStyleSheet(f"color:{color}; font-size:13px; font-weight:700;")

        # Progress bar centered at 50 = neutral, left = bearish, right = bullish
        bar_val = int(50 + signal * 50)
        bar_val = max(0, min(100, bar_val))
        self._bar.setValue(bar_val)
        bar_color = _GREEN if signal > 0.05 else _RED if signal < -0.05 else _GRAY
        self._bar.setStyleSheet(
            f"QProgressBar::chunk {{ background:{bar_color}; border-radius:3px; }}"
            f"QProgressBar {{ background:#1A2535; border-radius:3px; }}"
        )

        self._conf_lbl.setText(f"conf:{conf:.2f}")
        self._conf_lbl.setStyleSheet(
            f"color:{_LIGHT}; font-size:13px;" if conf >= 0.5 else f"color:{_GRAY}; font-size:13px;"
        )



# ── Intelligence Dashboard Page ───────────────────────────────
class IntelligencePage(QWidget):
    """
    AI Intelligence Dashboard — explains WHY the system is bullish/bearish.

    Sections:
      1. Meta-signal summary (orchestrator verdict)
      2. Signal contributors (each agent's vote + weight)
      3. Market context indicator (regime + confidence — full detail on Market Regime page)
      4. Why bullish/bearish explanation
      5. Active risk conditions
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._agent_cards:  dict[str, _AgentCard] = {}
        self._last_orch:    dict = {}
        self._last_status:  dict = {}
        self._build()

        # Live refresh via QTimer
        self._timer = QTimer(self)
        self._timer.setInterval(4000)
        self._timer.timeout.connect(self._refresh_all)
        self._timer.start()

        # EventBus subscriptions
        bus.subscribe(Topics.ORCHESTRATOR_SIGNAL, self._on_orchestrator)
        bus.subscribe(Topics.ORCHESTRATOR_VETO,   self._on_veto)
        bus.subscribe(Topics.REGIME_CHANGED,      self._on_regime_changed)

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(PageHeader(
            "AI Intelligence Dashboard",
            "Real-time multi-agent market analysis — why the system is bullish or bearish"
        ))

        # Scrollable content
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        cv = QVBoxLayout(content)
        cv.setContentsMargins(16, 16, 16, 16)
        cv.setSpacing(16)

        # ── 1. Meta-signal summary bar ─────────────────────────
        cv.addWidget(self._build_meta_bar())

        # ── 2. Signal contributors grid ───────────────────────
        cv.addWidget(self._build_contributors_section())

        # ── 3. Regime analysis panel ──────────────────────────
        cv.addWidget(self._build_regime_section())

        # ── 4. Narrative explanation ──────────────────────────
        cv.addWidget(self._build_narrative_section())

        # ── 5. Risk conditions ────────────────────────────────
        cv.addWidget(self._build_risk_section())

        cv.addStretch()
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

    # ── Section builders ──────────────────────────────────────

    def _build_meta_bar(self) -> QFrame:
        """Large orchestrator verdict banner."""
        frame = QFrame()
        frame.setObjectName("MetaBar")
        frame.setFixedHeight(90)
        frame.setStyleSheet(
            f"QFrame#MetaBar {{ background:{_BG}; border:1px solid #1E3A5F;"
            f" border-radius:8px; }}"
        )
        h = QHBoxLayout(frame)
        h.setContentsMargins(24, 12, 24, 12)
        h.setSpacing(32)

        # VERDICT section
        vv = QVBoxLayout()
        verdict_header = QLabel("SYSTEM VERDICT")
        verdict_header.setStyleSheet(f"color:{_GRAY}; font-size:13px; font-weight:700;")
        vv.addWidget(verdict_header)
        self._verdict_lbl = QLabel("NEUTRAL")
        font = QFont()
        font.setPointSize(18)
        font.setBold(True)
        self._verdict_lbl.setFont(font)
        self._verdict_lbl.setStyleSheet(f"color:{_GRAY};")
        vv.addWidget(self._verdict_lbl)
        h.addLayout(vv)

        # Meta signal value
        mv = QVBoxLayout()
        mv.addWidget(_sep_label("META SIGNAL"))
        self._meta_sig_lbl = QLabel("—")
        self._meta_sig_lbl.setStyleSheet(f"color:{_LIGHT}; font-size:20px; font-weight:700;")
        mv.addWidget(self._meta_sig_lbl)
        h.addLayout(mv)

        # Confidence
        cc = QVBoxLayout()
        cc.addWidget(_sep_label("CONFIDENCE"))
        self._meta_conf_lbl = QLabel("—")
        self._meta_conf_lbl.setStyleSheet(f"color:{_LIGHT}; font-size:16px;")
        cc.addWidget(self._meta_conf_lbl)
        h.addLayout(cc)

        # Regime bias
        rb = QVBoxLayout()
        rb.addWidget(_sep_label("BIAS"))
        self._bias_lbl = QLabel("neutral")
        self._bias_lbl.setStyleSheet(f"color:{_GRAY}; font-size:14px; font-weight:600;")
        rb.addWidget(self._bias_lbl)
        h.addLayout(rb)

        # Macro risk
        mr = QVBoxLayout()
        mr.addWidget(_sep_label("MACRO RISK"))
        self._macro_risk_lbl = QLabel("—")
        self._macro_risk_lbl.setStyleSheet(f"color:{_LIGHT}; font-size:14px;")
        mr.addWidget(self._macro_risk_lbl)
        h.addLayout(mr)

        h.addStretch()

        # Veto badge
        self._veto_lbl = QLabel("")
        self._veto_lbl.setStyleSheet(
            f"background:#7B0F1A; color:#FC8181; font-weight:700; font-size:13px;"
            f"padding:6px 12px; border-radius:4px;"
        )
        h.addWidget(self._veto_lbl)

        # Refresh button
        btn = QPushButton("↻")
        btn.setFixedSize(32, 32)
        btn.setToolTip("Refresh now")
        btn.clicked.connect(self._refresh_all)
        h.addWidget(btn)

        self._meta_frame = frame
        return frame

    def _build_contributors_section(self) -> QGroupBox:
        """Grid of agent cards showing each agent's current signal."""
        box = QGroupBox("Signal Contributors")
        box.setStyleSheet(
            f"QGroupBox {{ color:{_LIGHT}; font-weight:700; font-size:13px;"
            f" border:1px solid #1E3A5F; border-radius:6px; margin-top:8px; padding-top:12px; }}"
        )
        grid = QGridLayout(box)
        grid.setSpacing(8)

        agents = list(_AGENT_WEIGHTS.items())
        for i, (name, weight) in enumerate(agents):
            card = _AgentCard(name, weight)
            self._agent_cards[name] = card
            row, col = divmod(i, 4)
            grid.addWidget(card, row, col)

        return box

    def _build_regime_section(self) -> QGroupBox:
        """
        Compact market context indicator — regime name and confidence only.
        The full probability distribution and regime history live on the
        dedicated Market Regime page, avoiding duplication.
        """
        box = QGroupBox("Market Context")
        box.setStyleSheet(
            f"QGroupBox {{ color:{_LIGHT}; font-weight:700; font-size:13px;"
            f" border:1px solid #1E3A5F; border-radius:6px; margin-top:8px; padding-top:12px; }}"
        )
        h = QHBoxLayout(box)
        h.setContentsMargins(16, 8, 16, 12)
        h.setSpacing(16)

        h.addWidget(_sep_label("REGIME"))

        self._regime_lbl = QLabel("—")
        font = QFont()
        font.setPointSize(13)
        font.setBold(True)
        self._regime_lbl.setFont(font)
        self._regime_lbl.setStyleSheet(f"color:{_LIGHT};")
        h.addWidget(self._regime_lbl)

        dot = QLabel("·")
        dot.setStyleSheet(f"color:{_GRAY}; font-size:18px;")
        h.addWidget(dot)

        h.addWidget(_sep_label("CONFIDENCE"))

        self._regime_conf_lbl = QLabel("—")
        self._regime_conf_lbl.setStyleSheet(f"color:{_BLUE}; font-size:14px; font-weight:700;")
        h.addWidget(self._regime_conf_lbl)

        h.addStretch()

        hint = QLabel("Full analysis  →  Market Regime page")
        hint.setStyleSheet(f"color:{_GRAY}; font-size:12px; font-style:italic;")
        h.addWidget(hint)

        return box

    def _build_narrative_section(self) -> QGroupBox:
        """Human-readable explanation of why the system has its current view."""
        box = QGroupBox("Why Is the System Bullish / Bearish?")
        box.setStyleSheet(
            f"QGroupBox {{ color:{_LIGHT}; font-weight:700; font-size:13px;"
            f" border:1px solid #1E3A5F; border-radius:6px; margin-top:8px; padding-top:12px; }}"
        )
        v = QVBoxLayout(box)
        v.setSpacing(8)

        # Bullish drivers
        v.addWidget(_sep_label("BULLISH DRIVERS"))
        self._bull_lbl = QLabel("No bullish signals active")
        self._bull_lbl.setWordWrap(True)
        self._bull_lbl.setStyleSheet(f"color:{_GREEN}; font-size:13px; padding:4px;")
        v.addWidget(self._bull_lbl)

        # Bearish drivers
        v.addWidget(_sep_label("BEARISH DRIVERS"))
        self._bear_lbl = QLabel("No bearish signals active")
        self._bear_lbl.setWordWrap(True)
        self._bear_lbl.setStyleSheet(f"color:{_RED}; font-size:13px; padding:4px;")
        v.addWidget(self._bear_lbl)

        # Neutral/contradictory
        v.addWidget(_sep_label("CONTRADICTORY / NEUTRAL"))
        self._neutral_lbl = QLabel("—")
        self._neutral_lbl.setWordWrap(True)
        self._neutral_lbl.setStyleSheet(f"color:{_GRAY}; font-size:13px; padding:4px;")
        v.addWidget(self._neutral_lbl)

        return box

    def _build_risk_section(self) -> QGroupBox:
        """Active risk conditions and orchestrator risk state."""
        box = QGroupBox("Active Risk Conditions")
        box.setStyleSheet(
            f"QGroupBox {{ color:{_LIGHT}; font-weight:700; font-size:13px;"
            f" border:1px solid #1E3A5F; border-radius:6px; margin-top:8px; padding-top:12px; }}"
        )
        v = QVBoxLayout(box)

        self._risk_conditions_lbl = QLabel("No active risk conditions")
        self._risk_conditions_lbl.setWordWrap(True)
        self._risk_conditions_lbl.setStyleSheet(f"color:{_GRAY}; font-size:13px; padding:4px;")
        v.addWidget(self._risk_conditions_lbl)

        return box

    # ── Refresh logic ─────────────────────────────────────────

    def _refresh_all(self) -> None:
        """Pull latest data from coordinator and orchestrator."""
        self._refresh_agents()
        self._refresh_orchestrator()

    def _refresh_agents(self) -> None:
        try:
            from core.agents.agent_coordinator import get_coordinator
            coordinator = get_coordinator()
            status = coordinator.get_status()
            if not status:
                # No agents have produced data yet — nothing to update
                return
            self._last_status = status
            for name, card in self._agent_cards.items():
                if name in status:
                    card.refresh(status[name])
            self._update_narrative(status)
        except Exception:
            pass

    def _refresh_orchestrator(self) -> None:
        try:
            from core.orchestrator.orchestrator_engine import get_orchestrator
            orch = get_orchestrator()
            sig = orch.get_signal()
            d = sig.to_dict()
            self._last_orch = d
            self._update_meta_bar(d)
        except Exception:
            pass

    # ── Event handlers ────────────────────────────────────────

    def _on_orchestrator(self, event) -> None:
        data = event.data if hasattr(event, "data") else event
        if data:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, lambda d=data: self._apply_orchestrator(d))

    def _apply_orchestrator(self, data) -> None:
        self._last_orch = data
        self._update_meta_bar(data)

    def _on_veto(self, event) -> None:
        data = event.data if hasattr(event, "data") else {}
        active = data.get("veto", False) if isinstance(data, dict) else False
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, lambda a=active: self._veto_lbl.setText(
            "⊘  MACRO VETO — ALL TRADES BLOCKED" if a else ""
        ))

    def _on_regime_changed(self, event) -> None:
        data = event.data if hasattr(event, "data") else {}
        if isinstance(data, dict):
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, lambda d=data: self._apply_regime_changed(d))

    def _apply_regime_changed(self, data: dict) -> None:
        regime = data.get("new_regime", data.get("regime", "—"))
        regime_str = _REGIME_EMOJIS.get(regime, regime)
        self._regime_lbl.setText(regime_str)
        conf = data.get("confidence", 0.0)
        self._regime_conf_lbl.setText(f"{conf:.0%}")

    # ── Update methods ────────────────────────────────────────

    def _update_meta_bar(self, data: dict) -> None:
        meta = float(data.get("meta_signal", 0.0))
        conf = float(data.get("meta_confidence", 0.0))
        bias = data.get("regime_bias", "neutral")
        risk = float(data.get("macro_risk_score", 0.5))
        veto = data.get("macro_veto", False)

        # Verdict text
        if meta > 0.20:
            verdict, color = "BULLISH", _GREEN
        elif meta > 0.08:
            verdict, color = "MILDLY BULLISH", "#68D391"
        elif meta < -0.20:
            verdict, color = "BEARISH", _RED
        elif meta < -0.08:
            verdict, color = "MILDLY BEARISH", "#FC8181"
        else:
            verdict, color = "NEUTRAL", _GRAY

        self._verdict_lbl.setText(verdict)
        self._verdict_lbl.setStyleSheet(f"color:{color}; font-size:18px; font-weight:700;")
        self._meta_sig_lbl.setText(f"{meta:+.4f}")
        self._meta_sig_lbl.setStyleSheet(f"color:{color}; font-size:20px; font-weight:700;")
        self._meta_conf_lbl.setText(f"{conf:.0%}")
        self._bias_lbl.setText(bias)
        bias_color = _GREEN if bias == "risk_on" else _RED if bias == "risk_off" else _GRAY
        self._bias_lbl.setStyleSheet(f"color:{bias_color}; font-size:14px; font-weight:600;")
        self._macro_risk_lbl.setText(f"{risk:.2f}")
        risk_color = _RED if risk > 0.70 else _YELLOW if risk > 0.50 else _GREEN
        self._macro_risk_lbl.setStyleSheet(f"color:{risk_color}; font-size:14px;")
        self._veto_lbl.setText("⊘  MACRO VETO — TRADES BLOCKED" if veto else "")

        # Risk conditions
        conditions = []
        if veto:
            conditions.append("⊘  Macro Veto Active — new trades are blocked")
        if risk > 0.70:
            conditions.append(f"⚠  High macro risk score ({risk:.2f})")
        if bias == "risk_off":
            conditions.append("⚠  Macro signals are risk-off (institutional money moving to safety)")
        self._risk_conditions_lbl.setText(
            "\n".join(conditions) if conditions else "✓  No active risk conditions"
        )
        self._risk_conditions_lbl.setStyleSheet(
            f"color:{_RED}; font-size:13px; padding:4px;" if conditions
            else f"color:{_GREEN}; font-size:13px; padding:4px;"
        )

    def _update_narrative(self, status: dict) -> None:
        """Generate human-readable explanation of current signal drivers."""
        bull_drivers = []
        bear_drivers = []
        neutral_drivers = []

        agent_labels = {
            "funding_rate":    "Funding Rate",
            "order_book":      "Order Book Pressure",
            "options_flow":    "Options Flow (smart money)",
            "macro":           "Macro Environment",
            "social_sentiment":"Social Sentiment",
            "news":            "News Sentiment",
            "geopolitical":    "Geopolitical Risk",
            "sector_rotation": "Sector Rotation",
        }

        for agent_name, weight in _AGENT_WEIGHTS.items():
            if agent_name not in status:
                continue
            s = status[agent_name]
            sig  = float(s.get("signal", 0.0))
            conf = float(s.get("confidence", 0.0))
            stale = s.get("stale", True)
            if stale or conf < 0.30:
                continue

            label = agent_labels.get(agent_name, agent_name.replace("_", " ").title())
            weight_pct = f"{weight:.0%}"

            if sig > 0.10:
                driver_str = (
                    f"• {label} ({weight_pct} weight): "
                    f"signal={sig:+.3f} conf={conf:.0%}"
                )
                bull_drivers.append((sig * conf * weight, driver_str))
            elif sig < -0.10:
                driver_str = (
                    f"• {label} ({weight_pct} weight): "
                    f"signal={sig:+.3f} conf={conf:.0%}"
                )
                bear_drivers.append((abs(sig) * conf * weight, driver_str))
            else:
                driver_str = f"• {label}: neutral ({sig:+.3f})"
                neutral_drivers.append(driver_str)

        # Sort by contribution
        bull_drivers.sort(key=lambda x: x[0], reverse=True)
        bear_drivers.sort(key=lambda x: x[0], reverse=True)

        self._bull_lbl.setText(
            "\n".join(d[1] for d in bull_drivers) if bull_drivers
            else "No bullish signals currently active"
        )
        self._bear_lbl.setText(
            "\n".join(d[1] for d in bear_drivers) if bear_drivers
            else "No bearish signals currently active"
        )
        self._neutral_lbl.setText(
            "\n".join(neutral_drivers[:4]) if neutral_drivers else "—"
        )



# ── Helper widget ─────────────────────────────────────────────

def _sep_label(text: str) -> QLabel:
    """Small section header label."""
    lbl = QLabel(text)
    lbl.setStyleSheet(f"color:{_GRAY}; font-size:13px; font-weight:600;")
    return lbl
