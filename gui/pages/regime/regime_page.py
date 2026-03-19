# ============================================================
# NEXUS TRADER — Market Regime Analysis Page  (Phase 12)
#
# Dedicated page for market regime analysis showing:
#   - Current regime (rule-based + HMM ensemble)
#   - Regime history (last 50 classifications)
#   - HMM state probability distribution (live)
#   - Regime statistics (how long in each regime)
#   - Regime-to-performance correlation hint
# ============================================================
from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timezone

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QScrollArea, QGroupBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QPushButton, QSizePolicy, QGridLayout,
)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont

from gui.main_window import PageHeader
from core.event_bus import bus, Topics

logger = logging.getLogger(__name__)

_GREEN  = "#00FF88"
_RED    = "#FF3355"
_BLUE   = "#4299E1"
_YELLOW = "#F6AD55"
_GRAY   = "#8899AA"   # was #4A5568 — too dark to read on dark background
_LIGHT  = "#E2E8F0"   # was #C8D0E0 — slightly brighter white

_REGIME_COLORS = {
    "bull_trend":             "#00FF88",
    "bear_trend":             "#FF3355",
    "ranging":                "#4299E1",
    "volatility_expansion":   "#F6AD55",
    "volatility_compression": "#9F7AEA",
    "uncertain":              "#4A5568",
    "accumulation":           "#68D391",
    "distribution":           "#FC8181",
}

_REGIME_DESCRIPTIONS = {
    "bull_trend": (
        "Strong upward trend with high ADX and positive EMA slope. "
        "Trend-following strategies (momentum, breakout) are favoured. "
        "Risk-to-reward ratios tend to be positive."
    ),
    "bear_trend": (
        "Strong downward trend with high ADX and negative EMA slope. "
        "Short or hedging strategies are favoured. "
        "Avoid new long positions without strong confluence."
    ),
    "ranging": (
        "Low ADX with price oscillating between support/resistance. "
        "Mean-reversion and range strategies are preferred. "
        "Trend-following signals have higher false-positive rates."
    ),
    "volatility_expansion": (
        "Bollinger Band width expanding rapidly, indicating a volatility breakout. "
        "Breakout strategies can fire; high slippage risk. "
        "Use tighter risk management — moves can be sharp in either direction."
    ),
    "volatility_compression": (
        "Bollinger Bands tightly compressed — a squeeze is forming. "
        "Market is 'coiling' before a directional move. "
        "Wait for breakout confirmation before entering trend positions."
    ),
    "uncertain": (
        "Insufficient data or borderline indicator values prevent confident classification. "
        "No single regime dominates. Reduce position sizing and require higher confluence."
    ),
    "accumulation": (
        "Smart money accumulation phase — ADX low with volume rising and RSI in the 30–55 zone. "
        "Institutions quietly building positions. Favour cautious longs, tight stops, smaller sizes."
    ),
    "distribution": (
        "Smart money distribution phase — volume falling with price near 20-bar highs and RSI elevated. "
        "Institutions quietly offloading. Reduce long exposure; be alert for reversal signals."
    ),
}


class RegimePage(QWidget):
    """
    Market Regime Analysis page.
    Shows current regime, HMM probabilities, and regime history.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._history: deque = deque(maxlen=100)
        self._classifying = False   # guard: prevent concurrent fetches
        self._build()

        # Fast UI update timer (just syncs HMM fitted state label)
        self._timer = QTimer(self)
        self._timer.setInterval(5000)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()

        # Slower regime-fetch timer: re-classify every 5 minutes
        self._fetch_timer = QTimer(self)
        self._fetch_timer.setInterval(5 * 60 * 1000)
        self._fetch_timer.timeout.connect(self._classify_now)
        self._fetch_timer.start()

        bus.subscribe(Topics.REGIME_CHANGED, self._on_regime_changed)
        bus.subscribe(Topics.ORCHESTRATOR_SIGNAL, self._on_orchestrator)

        # Kick off the first classification after a short delay so the window
        # has time to finish painting before we do network I/O.
        QTimer.singleShot(1500, self._classify_now)

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(PageHeader(
            "Market Regime Analysis",
            "HMM + rule-based ensemble regime detection — current state and historical transitions"
        ))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        cv = QVBoxLayout(content)
        cv.setContentsMargins(16, 16, 16, 16)
        cv.setSpacing(16)

        # Row 1: Current regime card + description
        row1 = QHBoxLayout()
        row1.setSpacing(16)
        row1.addWidget(self._build_current_regime_card(), 1)
        row1.addWidget(self._build_regime_description_card(), 2)
        cv.addLayout(row1)

        # Row 2: HMM probability distribution
        cv.addWidget(self._build_hmm_probs_section())

        # Row 3: Regime history table
        cv.addWidget(self._build_history_section())

        # Row 4: Regime statistics
        cv.addWidget(self._build_stats_section())

        cv.addStretch()
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

    def _build_current_regime_card(self) -> QGroupBox:
        box = QGroupBox("Current Regime")
        box.setStyleSheet(self._box_style())
        v = QVBoxLayout(box)
        v.setSpacing(12)

        self._regime_dot = QLabel("●")
        self._regime_dot.setStyleSheet("color:#4A5568; font-size:32px;")
        self._regime_dot.setAlignment(Qt.AlignCenter)
        v.addWidget(self._regime_dot)

        self._regime_name_lbl = QLabel("Detecting...")
        font = QFont()
        font.setPointSize(13)
        font.setBold(True)
        self._regime_name_lbl.setFont(font)
        self._regime_name_lbl.setAlignment(Qt.AlignCenter)
        self._regime_name_lbl.setStyleSheet(f"color:{_LIGHT};")
        v.addWidget(self._regime_name_lbl)

        self._regime_since_lbl = QLabel("")
        self._regime_since_lbl.setAlignment(Qt.AlignCenter)
        self._regime_since_lbl.setStyleSheet(f"color:{_GRAY}; font-size:13px;")
        v.addWidget(self._regime_since_lbl)

        v.addWidget(_sep("CONFIDENCE"))
        self._regime_conf_lbl = QLabel("—")
        self._regime_conf_lbl.setAlignment(Qt.AlignCenter)
        self._regime_conf_lbl.setStyleSheet(f"color:{_BLUE}; font-size:22px; font-weight:700;")
        v.addWidget(self._regime_conf_lbl)

        v.addWidget(_sep("CLASSIFIER"))
        self._classifier_lbl = QLabel("HMM (60%) + Rule-based (40%)")
        self._classifier_lbl.setAlignment(Qt.AlignCenter)
        self._classifier_lbl.setStyleSheet(f"color:{_GRAY}; font-size:13px;")
        v.addWidget(self._classifier_lbl)

        v.addStretch()

        refresh_btn = QPushButton("↻  Refresh")
        refresh_btn.clicked.connect(self._classify_now)
        v.addWidget(refresh_btn)

        return box

    def _build_regime_description_card(self) -> QGroupBox:
        box = QGroupBox("Regime Meaning & Strategy Implications")
        box.setStyleSheet(self._box_style())
        v = QVBoxLayout(box)
        v.setSpacing(12)

        self._desc_lbl = QLabel(
            "No regime detected yet. The system needs at least 30 bars of indicator "
            "data to classify the current market state."
        )
        self._desc_lbl.setWordWrap(True)
        self._desc_lbl.setStyleSheet(f"color:{_LIGHT}; font-size:13px; line-height:150%;")
        v.addWidget(self._desc_lbl)

        v.addWidget(_sep("ACTIVE STRATEGIES IN THIS REGIME"))
        self._strategy_hint_lbl = QLabel("—")
        self._strategy_hint_lbl.setWordWrap(True)
        self._strategy_hint_lbl.setStyleSheet(f"color:{_YELLOW}; font-size:13px;")
        v.addWidget(self._strategy_hint_lbl)

        v.addWidget(_sep("RISK ADJUSTMENT"))
        self._risk_hint_lbl = QLabel("—")
        self._risk_hint_lbl.setWordWrap(True)
        self._risk_hint_lbl.setStyleSheet(f"color:{_LIGHT}; font-size:13px;")
        v.addWidget(self._risk_hint_lbl)

        v.addStretch()
        return box

    def _build_hmm_probs_section(self) -> QGroupBox:
        """HMM probability distribution for each regime state."""
        box = QGroupBox("HMM Regime Probability Distribution")
        box.setStyleSheet(self._box_style())
        v = QVBoxLayout(box)
        v.setSpacing(6)

        from core.regime.regime_classifier import ALL_REGIMES
        self._prob_rows: dict[str, tuple] = {}  # regime → (QLabel bar, QLabel pct)

        for regime in ALL_REGIMES:
            h = QHBoxLayout()
            h.setSpacing(8)

            color = _REGIME_COLORS.get(regime, _GRAY)
            name_lbl = QLabel(regime.replace("_", " ").title())
            name_lbl.setFixedWidth(210)
            name_lbl.setStyleSheet(f"color:{color}; font-size:13px; font-weight:600;")
            h.addWidget(name_lbl)

            # Bar (using a QFrame with width-based representation)
            bar_frame = QFrame()
            bar_frame.setFixedHeight(14)
            bar_frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            bar_frame.setStyleSheet(
                f"background:#1A2535; border-radius:7px;"
            )
            bar_container = QHBoxLayout(bar_frame)
            bar_container.setContentsMargins(0, 0, 0, 0)
            bar_container.setSpacing(0)

            bar_fill = QFrame()
            bar_fill.setFixedHeight(14)
            bar_fill.setFixedWidth(0)
            bar_fill.setStyleSheet(
                f"background:{color}; border-radius:7px;"
            )
            bar_container.addWidget(bar_fill)
            bar_container.addStretch()
            h.addWidget(bar_frame, 1)

            pct_lbl = QLabel("0%")
            pct_lbl.setFixedWidth(52)
            pct_lbl.setAlignment(Qt.AlignRight)
            pct_lbl.setStyleSheet(f"color:{_GRAY}; font-size:13px; font-weight:600;")
            h.addWidget(pct_lbl)

            self._prob_rows[regime] = (bar_fill, pct_lbl)
            v.addLayout(h)

        return box

    def _build_history_section(self) -> QGroupBox:
        box = QGroupBox("Regime History (last 50 detections)")
        box.setStyleSheet(self._box_style())
        v = QVBoxLayout(box)

        self._history_table = QTableWidget(0, 5)
        self._history_table.setHorizontalHeaderLabels([
            "Time", "Regime", "Confidence", "Classifier", "Duration"
        ])
        self._history_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._history_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._history_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._history_table.setMaximumHeight(200)
        self._history_table.setAlternatingRowColors(True)
        self._history_table.setStyleSheet(
            "QTableWidget { gridline-color:#1E2A3A; color:#E2E8F0; font-size:13px; }"
            "QHeaderView::section { background:#0D1B2A; color:#C8D0E0; "
            "border:none; padding:5px 4px; font-size:13px; font-weight:600; }"
        )
        v.addWidget(self._history_table)

        return box

    def _build_stats_section(self) -> QGroupBox:
        box = QGroupBox("Regime Statistics (current session)")
        box.setStyleSheet(self._box_style())
        grid = QGridLayout(box)
        grid.setSpacing(12)

        from core.regime.regime_classifier import ALL_REGIMES
        self._stat_labels: dict[str, QLabel] = {}

        for i, regime in enumerate(ALL_REGIMES):
            color = _REGIME_COLORS.get(regime, _GRAY)
            name = regime.replace("_", " ").title()
            row, col = divmod(i, 3)

            frame = QFrame()
            frame.setStyleSheet(
                f"background:#0D1B2A; border:1px solid {color}33; border-radius:6px;"
            )
            fh = QHBoxLayout(frame)
            fh.setContentsMargins(10, 6, 10, 6)

            name_lbl = QLabel(name)
            name_lbl.setStyleSheet(f"color:{color}; font-size:13px; font-weight:600;")
            fh.addWidget(name_lbl)
            fh.addStretch()

            val_lbl = QLabel("0×")
            val_lbl.setStyleSheet(f"color:{_LIGHT}; font-size:13px; font-weight:700;")
            fh.addWidget(val_lbl)
            self._stat_labels[regime] = val_lbl

            grid.addWidget(frame, row, col)

        return box

    # ── Refresh ───────────────────────────────────────────────

    def _refresh(self) -> None:
        """Lightweight: update HMM fitted label only (no network I/O)."""
        try:
            from core.regime.hmm_regime_classifier import hmm_classifier
            is_fitted = getattr(hmm_classifier, 'is_fitted', False)
            self._classifier_lbl.setText(
                "HMM (60%) + Rule-based (40%)" if is_fitted
                else "Rule-based only (HMM not fitted)"
            )
        except Exception:
            pass

    def _classify_now(self) -> None:
        """
        Trigger a background regime classification.
        Fetches BTC/USDT 1h bars from Binance public API (no auth needed),
        computes indicators, classifies the regime, and publishes REGIME_CHANGED.
        Safe to call from the UI thread — work happens in a daemon thread.
        """
        if self._classifying:
            return
        self._classifying = True
        self._regime_name_lbl.setText("Detecting...")
        self._classifier_lbl.setText("Fetching market data…")

        import threading
        t = threading.Thread(target=self._classify_thread, daemon=True)
        t.start()

    def _classify_thread(self) -> None:
        """Background worker: fetch → indicators → classify → publish."""
        try:
            import urllib.request
            import json as _json
            import pandas as pd

            # Binance public OHLCV — no API key required
            url = (
                "https://api.binance.com/api/v3/klines"
                "?symbol=BTCUSDT&interval=1h&limit=200"
            )
            req = urllib.request.Request(
                url,
                headers={"Accept": "application/json", "User-Agent": "NexusTrader/1.0"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = _json.loads(resp.read().decode())

            # Binance kline columns: open_time, open, high, low, close, volume, ...
            df = pd.DataFrame(raw, columns=[
                "timestamp", "open", "high", "low", "close", "volume",
                "close_time", "quote_vol", "trades",
                "taker_buy_base", "taker_buy_quote", "ignore",
            ])
            df = df[["timestamp", "open", "high", "low", "close", "volume"]].astype({
                "timestamp": "int64",
                "open": float, "high": float, "low": float,
                "close": float, "volume": float,
            })
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
            df = df.set_index("timestamp")

            if len(df) < 30:
                raise ValueError(f"Only {len(df)} bars returned — need at least 30")

            # Calculate indicators (same function the scanner uses)
            from core.features.indicator_library import calculate_all
            df = calculate_all(df)

            # Use HMM ensemble classifier (falls back to rule-based if not fitted)
            from core.regime.hmm_regime_classifier import hmm_classifier
            regime, confidence, probs = hmm_classifier.classify_combined(df)

            logger.info(
                "RegimePage: classified regime=%s conf=%.2f (BTC/USDT 1h, %d bars)",
                regime, confidence, len(df),
            )

            # Publish — _on_regime_changed will update the UI
            payload = {
                "new_regime":   regime,
                "confidence":   confidence,
                "regime_probs": probs,
                "classifier":   "HMM+rules" if getattr(hmm_classifier, "is_fitted", False)
                                else "rule-based",
                "source":       "BTC/USDT 1h",
            }
            bus.publish(Topics.REGIME_CHANGED, payload, source="regime_page")

        except Exception as exc:
            logger.warning("RegimePage: classification failed — %s", exc)
            # Update UI to show the error state
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, lambda: self._classifier_lbl.setText(
                f"Last fetch failed: {exc!s:.60}"
            ))
        finally:
            self._classifying = False

    # ── Event handlers ────────────────────────────────────────

    def _on_regime_changed(self, event) -> None:
        data = event.data if hasattr(event, "data") else {}
        if not isinstance(data, dict):
            return

        regime     = data.get("new_regime", data.get("regime", "uncertain"))
        confidence = float(data.get("confidence", 0.0))
        probs      = data.get("regime_probs", {})
        now_str    = datetime.now(timezone.utc).strftime("%H:%M:%S")

        # Update current regime card
        color = _REGIME_COLORS.get(regime, _GRAY)
        self._regime_dot.setStyleSheet(f"color:{color}; font-size:32px;")
        name_str = regime.replace("_", " ").title()
        self._regime_name_lbl.setText(name_str)
        self._regime_name_lbl.setStyleSheet(f"color:{color}; font-size:14px; font-weight:700;")
        self._regime_conf_lbl.setText(f"{confidence:.0%}")
        self._regime_since_lbl.setText(f"Detected at {now_str}")

        # Description card
        desc = _REGIME_DESCRIPTIONS.get(regime, "")
        self._desc_lbl.setText(desc)

        strategy_hints = {
            "bull_trend": "Trend Following, Momentum Breakout",
            "bear_trend": "Short strategies, reduced long exposure",
            "ranging": "Mean Reversion, Range trading",
            "volatility_expansion": "Breakout entries with tight stops",
            "volatility_compression": "Wait for squeeze release; prepare breakout orders",
            "uncertain": "Reduce size, require higher confluence threshold",
            "accumulation": "Gradual long accumulation, scale in on pullbacks",
            "distribution": "Reduce longs, hedge or flip to short on confirmation",
        }
        risk_hints = {
            "bull_trend": "Standard position sizing applies",
            "bear_trend": "Reduce long position sizes; tighten stops",
            "ranging": "Use ATR-based stops; avoid breakout trades",
            "volatility_expansion": "Increase ATR multiplier; widen stops",
            "volatility_compression": "Hold off on new positions; monitor for breakout",
            "uncertain": "Maximum caution: 50% of normal position size",
            "accumulation": "Use 75% normal position size; widen stops slightly",
            "distribution": "50% position size; tighten stops; watch for reversal",
        }
        self._strategy_hint_lbl.setText(strategy_hints.get(regime, "—"))
        self._risk_hint_lbl.setText(risk_hints.get(regime, "—"))

        # HMM probability bars
        if probs:
            max_regime = max(probs, key=lambda k: probs.get(k, 0))
            for r, (bar_fill, pct_lbl) in self._prob_rows.items():
                p = float(probs.get(r, 0.0))
                pct = int(p * 100)
                # Scale bar width relative to parent (max 400px conceptually)
                bar_width = max(2, int(p * 400))
                bar_fill.setFixedWidth(bar_width)
                is_best = (r == max_regime)
                pct_lbl.setText(f"{pct}%")
                pct_lbl.setStyleSheet(
                    f"color:{_REGIME_COLORS.get(r, _GRAY)}; font-size:13px; font-weight:700;"
                    if is_best else f"color:{_GRAY}; font-size:13px; font-weight:600;"
                )

        # Add to history
        self._history.append({
            "time":       now_str,
            "regime":     regime,
            "confidence": confidence,
            "classifier": data.get("classifier", "ensemble"),
            "duration":   "—",
        })
        self._update_history_table()
        self._update_stats()

    def _on_orchestrator(self, event) -> None:
        """Extract regime info from orchestrator signal."""
        data = event.data if hasattr(event, "data") else {}
        if not isinstance(data, dict):
            return
        regime = data.get("regime", "")
        if regime and not self._history:
            # Populate with latest regime from orchestrator on first load
            self._regime_name_lbl.setText(regime.replace("_", " ").title())
            color = _REGIME_COLORS.get(regime, _GRAY)
            self._regime_dot.setStyleSheet(f"color:{color}; font-size:32px;")
            self._regime_name_lbl.setStyleSheet(
                f"color:{color}; font-size:14px; font-weight:700;"
            )
            desc = _REGIME_DESCRIPTIONS.get(regime, "")
            self._desc_lbl.setText(desc)

    def _update_history_table(self) -> None:
        items = list(reversed(list(self._history)))[:50]
        self._history_table.setRowCount(len(items))
        for r, item in enumerate(items):
            color = _REGIME_COLORS.get(item["regime"], _GRAY)
            cols = [
                item["time"],
                item["regime"].replace("_", " ").title(),
                f"{item['confidence']:.0%}",
                item["classifier"],
                item["duration"],
            ]
            for c, val in enumerate(cols):
                cell = QTableWidgetItem(val)
                cell.setForeground(QColor(color if c == 1 else _LIGHT))
                self._history_table.setItem(r, c, cell)

    def _update_stats(self) -> None:
        from collections import Counter
        counts = Counter(item["regime"] for item in self._history)
        for regime, lbl in self._stat_labels.items():
            lbl.setText(f"{counts.get(regime, 0)}×")

    @staticmethod
    def _box_style() -> str:
        return (
            "QGroupBox { color:#E2E8F0; font-weight:700; font-size:13px;"
            " border:1px solid #1E3A5F; border-radius:6px;"
            " margin-top:8px; padding-top:12px; }"
        )


def _sep(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet("color:#8899AA; font-size:13px; font-weight:700; letter-spacing:0.5px;")
    return lbl
