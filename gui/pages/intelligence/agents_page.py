# ============================================================
# Intelligence Agents Page
# Full-page real-time status dashboard for all intelligence agents.
# Shows per-agent signal, confidence, staleness, and error counts,
# plus the OrchestratorEngine meta-signal and veto state.
#
# Moved from Market Scanner (Tab 3) to DASHBOARDS section so that
# operational monitoring is co-located with other observability tools.
# ============================================================

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFrame, QLabel,
    QScrollArea, QPushButton,
)
from PySide6.QtCore import Qt, QTimer

from gui.main_window import PageHeader

import logging
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Per-agent interpretation data
# ─────────────────────────────────────────────────────────────

_AGENT_MEANINGS: dict[str, tuple[str, str, str]] = {
    "funding_rate": (
        "Shorts paying longs → bearish crowding, squeeze risk for shorts",
        "Longs paying shorts → leveraged longs overheating, pullback risk",
        "Balanced funding — no directional crowding in perpetuals",
    ),
    "order_book": (
        "Bid-side dominated → active buying pressure at current levels",
        "Ask-side dominated → sellers in control, distribution pressure",
        "Balanced order book — buyers and sellers roughly matched",
    ),
    "options_flow": (
        "Call-heavy options activity → institutional bullish positioning",
        "Put-heavy flow → hedging or directional bearish bets in options",
        "Mixed options flow — no clear directional bias from options market",
    ),
    "macro": (
        "Risk-on macro environment → DXY falling, equities rising, yields stable/declining",
        "Risk-off conditions → DXY strengthening, equities falling, yield spike detected",
        "Neutral macro backdrop — indicators split, no dominant risk-on or risk-off bias",
    ),
    "social_sentiment": (
        "Positive crowd sentiment → retail optimism, FOMO building",
        "Fear and negativity dominant → panic or bearish crowd psychology",
        "Mixed sentiment — community divided, no consensus direction",
    ),
    "news": (
        "Positive news catalysts → regulatory clarity, adoption, partnerships",
        "Negative news flow → FUD, regulatory threats, hacks, or macro shocks",
        "Neutral news environment — no significant catalysts detected",
    ),
    "geopolitical": (
        "Stable geopolitical environment → low global risk premium",
        "Geopolitical risk event detected → safe-haven demand, risk-off flows",
        "Stable geopolitical background — no active risk events",
    ),
    "sector_rotation": (
        "Capital flowing into crypto sector → institutional allocation increasing",
        "Capital rotating out of crypto → institutional de-risking or sector exit",
        "Flat sector rotation — crypto holding market share vs other assets",
    ),
}

_UNKNOWN_MEANING = (
    "Bullish signal from this agent",
    "Bearish signal from this agent",
    "Neutral — no directional bias detected",
)

# Per-agent explanation shown while the agent hasn't produced a signal yet.
_AGENT_AWAITING: dict[str, str] = {
    "funding_rate": (
        "Waiting for exchange data. This agent measures the cost of holding perpetual futures "
        "positions. When longs pay shorts (positive rate), the market is overheated on the long "
        "side — a contrarian bearish signal. When shorts pay longs (negative rate), short "
        "crowding creates squeeze risk — a contrarian bullish signal."
    ),
    "order_book": (
        "Fetching live order book. This agent analyses the real-time queue of buy and sell limit "
        "orders sitting at current price levels. Heavy bid-side volume signals active buying "
        "pressure; heavy ask-side volume signals distribution pressure from sellers."
    ),
    "options_flow": (
        "Fetching Deribit options data for BTC and ETH. This agent reads institutional hedging "
        "behaviour via put/call ratios, the max pain strike (where option sellers lose least), "
        "and implied volatility skew between puts and calls."
    ),
    "macro": (
        "Collecting macro indicators. This agent aggregates Fear & Greed index, US Dollar Index "
        "(DXY), 10-year Treasury yields, S&P 500 momentum, and BTC dominance to assess whether "
        "global risk appetite favours or disfavours crypto."
    ),
    "social_sentiment": (
        "Gathering social data. This agent measures crowd psychology by analysing Reddit, Twitter, "
        "CoinGecko trending coins, and the Fear & Greed index. Extreme fear often signals buying "
        "opportunities; extreme greed signals overheating."
    ),
    "news": (
        "Scanning news sources. This agent applies FinBERT (AI sentiment model) or VADER to recent "
        "crypto headlines from multiple sources. Positive catalysts (ETF approvals, partnerships) "
        "push the signal up; FUD, hacks, or regulatory threats push it down."
    ),
    "geopolitical": (
        "Monitoring geopolitical feeds. This agent scans GDELT global news and crypto headlines for "
        "high-risk keywords (hacks, bans, sanctions, investigations) and weights them by entity "
        "importance (SEC, Tether, China). Risk events push the signal bearish."
    ),
    "sector_rotation": (
        "Loading sector ETF data. This agent tracks capital flows between risk-on sectors (QQQ, "
        "XLK, ARKK) and risk-off sectors (GLD, TLT, XLU) using 5-day momentum. Capital rotating "
        "into risk-on assets is bullish for crypto; rotation into gold/bonds is bearish."
    ),
}


def _make_assessment(agent_name: str, signal: float, conf: float,
                     errors: int) -> tuple[str, str]:
    """
    Return (visible_text, full_tooltip) interpreting signal + confidence
    for a specific intelligence agent.

    visible_text — shown directly in the table cell (word-wrapped)
    full_tooltip — hover text with numeric detail
    """
    key = agent_name.lower()
    pos_text, neg_text, neu_text = _AGENT_MEANINGS.get(key, _UNKNOWN_MEANING)

    # ── No data yet ────────────────────────────────────────────
    if conf == 0.0 and signal == 0.0:
        awaiting = _AGENT_AWAITING.get(key,
            "Agent is initialising — waiting for data from its source.")
        return awaiting, awaiting

    # ── Direction bracket ──────────────────────────────────────
    if signal > 0.30:
        direction = "STRONGLY BULLISH"
        meaning   = pos_text
        strength  = f"Strong bullish signal ({signal:+.3f})"
    elif signal > 0.10:
        direction = "MILDLY BULLISH"
        meaning   = pos_text
        strength  = f"Mild bullish signal ({signal:+.3f})"
    elif signal > 0.05:
        direction = "SLIGHTLY BULLISH"
        meaning   = pos_text
        strength  = f"Slight bullish lean ({signal:+.3f})"
    elif signal < -0.30:
        direction = "STRONGLY BEARISH"
        meaning   = neg_text
        strength  = f"Strong bearish signal ({signal:+.3f})"
    elif signal < -0.10:
        direction = "MILDLY BEARISH"
        meaning   = neg_text
        strength  = f"Mild bearish signal ({signal:+.3f})"
    elif signal < -0.05:
        direction = "SLIGHTLY BEARISH"
        meaning   = neg_text
        strength  = f"Slight bearish lean ({signal:+.3f})"
    else:
        direction = "NEUTRAL"
        meaning   = neu_text
        strength  = f"No directional bias ({signal:+.3f})"

    # ── Confidence qualifier ───────────────────────────────────
    if conf >= 0.75:
        conf_label = "High conviction"
        conf_note  = (f"Confidence {conf:.0%} — the signal is well-supported by "
                      f"recent, consistent data. Weight this agent's input heavily.")
    elif conf >= 0.50:
        conf_label = "Moderate conviction"
        conf_note  = (f"Confidence {conf:.0%} — treat as a contributing factor. "
                      f"The data is present but not overwhelmingly consistent.")
    elif conf >= 0.25:
        conf_label = "Low conviction"
        conf_note  = (f"Confidence {conf:.0%} — signal is noisy or based on sparse "
                      f"data. Use with caution alongside higher-confidence agents.")
    else:
        conf_label = "Very low conviction"
        conf_note  = (f"Confidence {conf:.0%} — insufficient or unstable data. "
                      f"This agent's signal carries very little weight right now.")

    error_note = f"  ⚠ {errors} error(s) recorded — data may be incomplete." if errors > 0 else ""

    visible_text = f"{conf_label} · {direction}\n{meaning}"
    full_tooltip = (
        f"{strength}  |  Confidence: {conf:.2f}\n\n"
        f"What this means:\n{meaning}\n\n"
        f"{conf_note}{error_note}"
    )
    return visible_text, full_tooltip


# ─────────────────────────────────────────────────────────────
# _AgentRow — single-agent status row widget
# ─────────────────────────────────────────────────────────────

class _AgentRow(QFrame):
    """Single-agent status row widget."""

    _ROW_STYLE = (
        "QFrame { background:#0A0E1A; border-bottom:1px solid #141E2E; }"
        "QFrame:hover { background:#0D1320; }"
    )

    def __init__(self, agent_name: str, parent=None):
        super().__init__(parent)
        self.setStyleSheet(self._ROW_STYLE)
        self.setFixedHeight(62)
        self._build(agent_name)

    def _build(self, name: str) -> None:
        h = QHBoxLayout(self)
        h.setContentsMargins(12, 4, 12, 4)
        h.setSpacing(16)

        # Agent name
        display = name.replace("_", " ").title()
        self._name_lbl = QLabel(display)
        self._name_lbl.setFixedWidth(160)
        self._name_lbl.setStyleSheet("color:#C8D8E8; font-size:13px; font-weight:600;")
        h.addWidget(self._name_lbl)

        # Status dot
        self._status_lbl = QLabel("◌  Initialising")
        self._status_lbl.setFixedWidth(90)
        self._status_lbl.setStyleSheet("color:#4A5568; font-size:13px;")
        h.addWidget(self._status_lbl)

        # Signal arrow
        self._signal_lbl = QLabel("—")
        self._signal_lbl.setStyleSheet("color:#4A5568; font-size:14px; font-weight:700;")
        self._signal_lbl.setFixedWidth(70)
        h.addWidget(self._signal_lbl)

        # Confidence
        self._conf_lbl = QLabel("conf: —")
        self._conf_lbl.setFixedWidth(80)
        h.addWidget(self._conf_lbl)

        # AI Assessment
        self._assess_lbl = QLabel("Awaiting data")
        self._assess_lbl.setStyleSheet("color:#6B7FA3; font-size:13px;")
        self._assess_lbl.setWordWrap(True)
        self._assess_lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        h.addWidget(self._assess_lbl, 1)

        # Last updated
        self._updated_lbl = QLabel("never")
        self._updated_lbl.setFixedWidth(72)
        self._updated_lbl.setStyleSheet("color:#8899AA; font-size:13px;")
        h.addWidget(self._updated_lbl)

        # Error count
        self._err_lbl = QLabel("")
        self._err_lbl.setFixedWidth(60)
        h.addWidget(self._err_lbl)

    def refresh(self, status: dict) -> None:
        running = status.get("running", False)
        stale   = status.get("stale", True)
        signal  = status.get("signal", 0.0)
        conf    = status.get("confidence", 0.0)
        updated = status.get("updated_at", "never")
        errors  = status.get("errors", 0)

        # Status
        if not running:
            self._status_lbl.setText("◌  Stopped")
            self._status_lbl.setStyleSheet("color:#4A5568;")
        elif stale:
            self._status_lbl.setText("◌  Stale")
            self._status_lbl.setStyleSheet("color:#F6AD55;")
        else:
            self._status_lbl.setText("●  Live")
            self._status_lbl.setStyleSheet("color:#48BB78;")

        # Signal
        if conf > 0:
            color = "#48BB78" if signal > 0.05 else "#FC8181" if signal < -0.05 else "#8899AA"
            arrow = "▲" if signal > 0.05 else "▼" if signal < -0.05 else "—"
            self._signal_lbl.setText(f"{arrow} {signal:+.3f}")
            self._signal_lbl.setStyleSheet(f"color:{color}; font-weight:600;")
        else:
            self._signal_lbl.setText("—")
            self._signal_lbl.setStyleSheet("color:#4A5568;")

        self._conf_lbl.setText(f"conf: {conf:.2f}")
        self._conf_lbl.setStyleSheet(
            "color:#A0AEC0;" if conf >= 0.5 else "color:#4A5568;"
        )

        # AI Assessment
        agent_key = self._name_lbl.text().lower().replace(" ", "_")
        short_txt, tooltip = _make_assessment(agent_key, signal, conf, errors)

        if conf == 0.0 and signal == 0.0:
            assess_color = "#E8EBF0"
        elif signal > 0.05:
            assess_color = "#48BB78"
        elif signal < -0.05:
            assess_color = "#FC8181"
        else:
            assess_color = "#8899AA"

        self._assess_lbl.setText(short_txt)
        self._assess_lbl.setStyleSheet(f"color:{assess_color}; font-size:13px;")
        self._assess_lbl.setToolTip(tooltip)

        # Timestamp
        if "T" in updated:
            self._updated_lbl.setText(updated.split("T")[1][:8])
        else:
            self._updated_lbl.setText(updated[:16])

        if errors > 0:
            self._err_lbl.setText(f"⚠ {errors}")
            self._err_lbl.setStyleSheet("color:#FC8181; font-size:13px;")
        else:
            self._err_lbl.setText("")


# ─────────────────────────────────────────────────────────────
# AgentsDashboard — scrollable agent grid + orchestrator bar
# ─────────────────────────────────────────────────────────────

class AgentsDashboard(QWidget):
    """
    Real-time status dashboard for all intelligence agents.
    Shows per-agent signal, confidence, staleness, and error counts,
    plus the OrchestratorEngine meta-signal and veto state.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._agent_rows: dict[str, _AgentRow] = {}
        self._build()

        self._timer = QTimer(self)
        self._timer.setInterval(5000)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()

        from core.event_bus import bus, Topics
        bus.subscribe(Topics.ORCHESTRATOR_SIGNAL, self._on_orchestrator)
        bus.subscribe(Topics.ORCHESTRATOR_VETO,   self._on_veto)

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Scrollable content wrapper ──────────────────────────
        content = QWidget()
        content_lay = QVBoxLayout(content)
        content_lay.setContentsMargins(16, 12, 16, 12)
        content_lay.setSpacing(12)

        scroll = QScrollArea()
        scroll.setWidget(content)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setStyleSheet(
            "QScrollArea { background:#060B14; border:none; }"
            "QScrollBar:vertical { background:#060B14; width:8px; margin:0; border-radius:4px; }"
            "QScrollBar::handle:vertical { background:#2A3A52; border-radius:4px; min-height:24px; }"
            "QScrollBar::handle:vertical:hover { background:#3D5270; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical"
            " { height:0; width:0; border:none; background:none; }"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background:none; }"
        )
        root.addWidget(scroll, 1)

        # ── Orchestrator summary bar ────────────────────────────
        orch_frame = QFrame()
        orch_frame.setObjectName("OrchestratorBar")
        orch_frame.setFixedHeight(60)
        orch_frame.setStyleSheet(
            "QFrame#OrchestratorBar { background:#0D1B2A; border: 1px solid #1E3A5F;"
            " border-radius:6px; }"
        )
        oh = QHBoxLayout(orch_frame)
        oh.setContentsMargins(16, 8, 16, 8)
        oh.setSpacing(24)

        oh.addWidget(QLabel("ORCHESTRATOR"))

        self._orch_signal_lbl = QLabel("meta: —")
        self._orch_signal_lbl.setStyleSheet("font-size:14px; font-weight:700;")
        oh.addWidget(self._orch_signal_lbl)

        self._orch_bias_lbl = QLabel("bias: neutral")
        oh.addWidget(self._orch_bias_lbl)

        self._orch_risk_lbl = QLabel("risk: 0.50")
        oh.addWidget(self._orch_risk_lbl)

        self._orch_veto_lbl = QLabel("")
        self._orch_veto_lbl.setStyleSheet(
            "color:#FC8181; font-weight:700; font-size:13px;"
        )
        oh.addWidget(self._orch_veto_lbl)
        oh.addStretch()

        content_lay.addWidget(orch_frame)

        # ── Column headers ──────────────────────────────────────
        hdr = QFrame()
        hh  = QHBoxLayout(hdr)
        hh.setContentsMargins(12, 0, 12, 0)
        hh.setSpacing(16)

        for text, width in [
            ("AGENT", 160), ("STATUS", 90), ("SIGNAL", 70),
            ("CONFIDENCE", 80), ("AI ASSESSMENT", -1), ("LAST UPDATE", 72), ("ERRORS", 60),
        ]:
            lbl = QLabel(text)
            lbl.setStyleSheet("color:#8899AA; font-size:13px; font-weight:600;")
            if width > 0:
                lbl.setFixedWidth(width)
            hh.addWidget(lbl, 0 if width > 0 else 1)
        content_lay.addWidget(hdr)

        # ── Agent rows (enabled agents only) ────────────────────
        # Config key → (settings_key, default_if_missing).
        # Agents without a key are always shown (no disable gate in coordinator).
        _AGENT_GATE: dict[str, tuple[str, bool]] = {
            "funding_rate":     ("agents.funding_enabled",          True),
            "order_book":       ("agents.orderbook_enabled",        False),
            "options_flow":     ("agents.options_enabled",          False),
            "social_sentiment": ("agents.social_sentiment_enabled", False),
            "sector_rotation":  ("agents.sector_rotation_enabled",  False),
        }
        _ALL_AGENT_NAMES = [
            "funding_rate", "order_book", "options_flow",
            "macro", "social_sentiment", "news",
            "geopolitical", "sector_rotation",
        ]

        def _is_enabled(name: str) -> bool:
            if name not in _AGENT_GATE:
                return True  # no disable gate — always active
            key, default = _AGENT_GATE[name]
            try:
                from config.settings import settings as _s
                return bool(_s.get(key, default))
            except Exception:
                return default

        agent_names = [n for n in _ALL_AGENT_NAMES if _is_enabled(n)]
        for name in agent_names:
            row = _AgentRow(name)
            self._agent_rows[name] = row
            content_lay.addWidget(row)

        content_lay.addStretch()

        # ── Legend panel ────────────────────────────────────────
        legend = QFrame()
        legend.setStyleSheet(
            "QFrame { background:#0A1628; border:1px solid #1E3A5F; border-radius:6px; }"
        )
        lg = QVBoxLayout(legend)
        lg.setContentsMargins(16, 10, 16, 10)
        lg.setSpacing(6)

        title_lbl = QLabel("How to read the Orchestrator numbers")
        title_lbl.setStyleSheet("color:#E8EBF0; font-size:13px; font-weight:700;")
        lg.addWidget(title_lbl)

        explanations = [
            ("<b>Meta</b> (e.g. +0.184)",
             "The weighted aggregate signal from all running agents, ranging from −1.0 to +1.0. "
             "Positive = net bullish bias across all intelligence sources; negative = net bearish. "
             "Values above +0.30 or below −0.30 are strong enough to influence scanner trade entry "
             "thresholds. Values near 0 mean agents are split or uncertain."),
            ("<b>Conf</b> (e.g. 0.27)",
             "Meta-confidence, from 0.00 to 1.00. Measures how much usable, recent data "
             "the agents collectively provided. Low conf (< 0.30) means few agents have live data "
             "— the meta signal carries less weight. High conf (> 0.70) means most agents are "
             "active and the meta signal is reliable."),
            ("<b>Bias</b> — risk_on / risk_off / neutral",
             "The macro regime bias, derived from three primary indicators: (1) DXY trend — a "
             "falling US Dollar signals capital rotating into risk assets (risk_on); a rising "
             "Dollar signals a flight to safety (risk_off). (2) Equity market trend — rising "
             "equities confirm broad risk appetite (risk_on); falling equities signal defensive "
             "conditions (risk_off). (3) Treasury yield momentum — gradually declining or stable "
             "yields are supportive (risk_on); a rapid yield spike tightens financial conditions "
             "(risk_off). Each indicator casts a +1 or −1 vote; a net score of ≥ +1 = risk_on, "
             "≤ −1 = risk_off, otherwise neutral. The scanner raises the confluence threshold "
             "by +0.10 when bias is risk_off to require stronger signals before entering trades."),
            ("<b>Risk</b> (e.g. 0.25)",
             "Macro risk score from 0.00 (no risk) to 1.00 (extreme risk). Derived from the Macro "
             "agent's Fear & Greed, DXY, yield curve, and equity readings. Above 0.75 a macro veto "
             "can block new trades entirely. The CRASH RISK MONITOR on the Dashboard uses the same "
             "score scaled to 0–10."),
        ]
        for label_html, desc in explanations:
            row_w = QFrame()
            row_l = QHBoxLayout(row_w)
            row_l.setContentsMargins(0, 2, 0, 2)
            row_l.setSpacing(10)
            key_lbl = QLabel(label_html)
            key_lbl.setStyleSheet("color:#E8EBF0; font-size:13px;")
            key_lbl.setFixedWidth(140)
            key_lbl.setTextFormat(Qt.RichText)
            row_l.addWidget(key_lbl)
            val_lbl = QLabel(desc)
            val_lbl.setStyleSheet("color:#E8EBF0; font-size:13px;")
            val_lbl.setWordWrap(True)
            row_l.addWidget(val_lbl, 1)
            lg.addWidget(row_w)

        content_lay.addWidget(legend)

        # Refresh button
        btn = QPushButton("↻  Refresh Now")
        btn.setFixedWidth(140)
        btn.clicked.connect(self._refresh)
        content_lay.addWidget(btn)

    def _refresh(self) -> None:
        """Pull latest status from AgentCoordinator."""
        try:
            from core.agents.agent_coordinator import get_coordinator
            coordinator = get_coordinator()
            status = coordinator.get_status()
            if not status:
                return
            for agent_name, row in self._agent_rows.items():
                if agent_name in status:
                    row.refresh(status[agent_name])
        except Exception:
            pass

    def _on_orchestrator(self, event) -> None:
        """Update orchestrator summary bar."""
        try:
            d    = event.data if hasattr(event, "data") and isinstance(event.data, dict) else (event if isinstance(event, dict) else {})
            meta = d.get("meta_signal", 0.0)
            conf = d.get("meta_confidence", 0.0)
            bias = d.get("regime_bias", "neutral")
            risk = d.get("macro_risk_score", 0.5)
            veto = d.get("macro_veto", False)
            color = "#48BB78" if meta > 0.1 else "#FC8181" if meta < -0.1 else "#8899AA"
            self._orch_signal_lbl.setText(f"meta: {meta:+.3f}  conf:{conf:.2f}")
            self._orch_signal_lbl.setStyleSheet(
                f"font-size:14px; font-weight:700; color:{color};"
            )
            self._orch_bias_lbl.setText(f"bias: {bias}")
            self._orch_risk_lbl.setText(f"risk: {risk:.2f}")
            self._orch_veto_lbl.setText("⊘ MACRO VETO ACTIVE" if veto else "")
        except Exception:
            pass

    def _on_veto(self, event) -> None:
        d      = event.data if hasattr(event, "data") and isinstance(event.data, dict) else (event if isinstance(event, dict) else {})
        active = d.get("veto", False)
        self._orch_veto_lbl.setText("⊘ MACRO VETO ACTIVE" if active else "")


# ─────────────────────────────────────────────────────────────
# IntelligenceAgentsPage — top-level page widget
# ─────────────────────────────────────────────────────────────

class IntelligenceAgentsPage(QWidget):
    """
    Standalone Dashboard-section page for Intelligence Agent monitoring.
    Wraps AgentsDashboard with the standard PageHeader.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(PageHeader(
            "Intelligence Agents",
            "Real-time per-agent signal status  ·  Orchestrator meta-signal  ·  "
            "Confidence, staleness and error monitoring"
        ))

        self._dashboard = AgentsDashboard()
        root.addWidget(self._dashboard, 1)
