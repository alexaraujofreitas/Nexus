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
        "A sustained, high-momentum upward trend has been confirmed across multiple technical "
        "layers. ADX (Average Directional Index) is above the 25 threshold, distinguishing a "
        "genuine directional move from sideways noise. EMA-9 has crossed above EMA-21 and is "
        "widening, confirming trend persistence. RSI is typically between 50 and 75, reflecting "
        "bullish momentum that has not yet reached overbought exhaustion. Volume is rising on "
        "up-candles, which reinforces the direction. Both the HMM and rule-based classifiers "
        "agree on this state — the market is trending, not oscillating."
    ),
    "bear_trend": (
        "A sustained, high-momentum downward trend has been confirmed across multiple technical "
        "layers. ADX (Average Directional Index) is above the 25 threshold, confirming trend "
        "strength — this is a real directional move, not a sideways chop. EMA-9 has crossed "
        "below EMA-21 and is widening, indicating the bearish structure is active and deepening. "
        "RSI is typically below 45, confirming sustained selling momentum rather than a brief "
        "dip. The HMM component detects a low-return, moderate-to-high volatility environment, "
        "which is the statistical fingerprint of a downtrend. Both classifiers agree on this "
        "state, which is why confidence is high."
    ),
    "ranging": (
        "Price is moving sideways with no clear directional trend. ADX is below 20, meaning "
        "neither bulls nor bears have sufficient force to establish a sustained move. EMAs are "
        "flat or closely intertwined, and price oscillates between identifiable support and "
        "resistance levels. RSI tends to cycle between 35 and 65 without committing to either "
        "extreme. The HMM detects a low-variance, mean-reverting return sequence consistent "
        "with a consolidation phase. The market is 'taking a breath' — the next significant "
        "move will likely be determined by an external catalyst or a breakout from the range."
    ),
    "volatility_expansion": (
        "A volatility breakout is underway — Bollinger Band width is expanding sharply, "
        "signalling that price is leaving a period of compression or consolidation at high "
        "speed. ATR (Average True Range) has spiked relative to its 20-period average. The "
        "direction of the move is not guaranteed; volatility expansion can accompany both "
        "upside breakouts and crash events. The HMM identifies unusually large return "
        "magnitudes in recent bars, which is the statistical signature of an expansion phase. "
        "Slippage is elevated and order fills can be poor — risk management is critical."
    ),
    "volatility_compression": (
        "The market is coiling — Bollinger Bands are contracting to unusually narrow width, "
        "indicating that volatility has collapsed and a high-energy move is likely building. "
        "ATR is well below its 20-period average. This state is sometimes called a 'squeeze'. "
        "Price is compressing into a tight range as buyers and sellers reach an impasse. "
        "Historically, prolonged compressions resolve with sharp directional moves in either "
        "direction. The HMM identifies very small, low-variance returns over recent bars. "
        "The correct response is to wait: do not enter trend positions before the direction "
        "of the breakout is confirmed."
    ),
    "uncertain": (
        "The classifier cannot assign high confidence to any single regime. Indicators are "
        "contradictory: ADX may be borderline, EMAs may be tangled without a clear cross, "
        "and/or there is insufficient data (fewer than 30 bars) to compute reliable indicators. "
        "The HMM probability distribution is flat — no single state dominates. This happens "
        "most commonly during low-liquidity sessions, immediately after major news events "
        "that disrupt the normal indicator relationships, or during the first few minutes "
        "after a restart before enough bars accumulate. It is NOT necessarily a bad sign — "
        "it means the system is honest about uncertainty rather than forcing a false verdict."
    ),
    "accumulation": (
        "Smart money appears to be quietly building positions. ADX is low (no strong trend "
        "yet), but volume is rising on otherwise unremarkable price candles — institutions "
        "absorbing sell-side supply without pushing price up aggressively. RSI is in the "
        "30–55 zone, indicating neither overbought nor deeply oversold conditions. This "
        "divergence between rising volume and flat price is the classic accumulation signal. "
        "The HMM identifies a low-volatility, mildly positive drift pattern. This regime "
        "often precedes a bull_trend transition once the supply has been fully absorbed and "
        "a catalyst triggers the directional move."
    ),
    "distribution": (
        "Smart money appears to be quietly offloading positions. Price is near its 20-bar "
        "high and RSI is elevated (typically 55–75), yet volume is declining — institutions "
        "are selling into retail strength. The classic distribution signature is 'rising "
        "price on falling volume', meaning fewer participants are willing to buy at these "
        "levels. The HMM identifies a low-volatility environment with slowing upward drift. "
        "This regime often precedes a bear_trend or rapid correction once institutional "
        "selling pressure accelerates and retail buying exhausts."
    ),
}

# How the classifier arrived at the regime verdict — shown in the "How Detected" section
_REGIME_DETECTION = {
    "bull_trend": (
        "Detection path: Rule-based check confirmed ADX > 25 (strong trend) + EMA-9 above "
        "EMA-21 (bullish crossover) + RSI 50–75 (bullish momentum). The HMM independently "
        "classified this as a high-return, moderate-volatility state (the HMM's 'bull' state "
        "in its internal 4-state model). The ensemble blends these at 60% HMM + 40% "
        "rule-based. Both agreed → high confidence. Source data: BTC/USDT 1h bars from "
        "Binance public API (200 bars, no auth required)."
    ),
    "bear_trend": (
        "Detection path: Rule-based check confirmed ADX > 25 (strong trend) + EMA-9 below "
        "EMA-21 (bearish crossover) + RSI below 45 (bearish momentum). The HMM "
        "independently classified this as a low-return, moderate-to-high volatility state "
        "(the HMM's 'bear' state in its internal 4-state model). The ensemble blends at "
        "60% HMM + 40% rule-based. When both methods independently assign the same label, "
        "the confidence score rises toward 100%. A 100% reading means the classifier "
        "has very high certainty — all indicator conditions are clearly met and the HMM "
        "probability mass is heavily concentrated on this state. Source: BTC/USDT 1h, "
        "200 bars. This page re-classifies every 5 minutes."
    ),
    "ranging": (
        "Detection path: Rule-based check confirmed ADX < 20 (no trend) + EMAs flat or "
        "crossing without sustained separation + RSI cycling 35–65 without extreme readings. "
        "The HMM classified this as a mean-reverting, low-drift state with low variance. "
        "Ensemble blend: 60% HMM + 40% rule-based. Confidence reflects the degree to "
        "which both methods agree — high confidence means both the ADX and HMM clearly "
        "indicate range behaviour; low confidence means indicators are borderline. "
        "Source: BTC/USDT 1h, 200 bars."
    ),
    "volatility_expansion": (
        "Detection path: Bollinger Band width (current / 20-bar average) exceeded the "
        "expansion threshold. ATR spiked relative to its rolling average. The HMM detected "
        "an unusually large-return state in recent bars. Both the magnitude of returns and "
        "the rate of Bollinger width change contributed to the classification. Ensemble: "
        "60% HMM + 40% rule-based. Source: BTC/USDT 1h, 200 bars."
    ),
    "volatility_compression": (
        "Detection path: Bollinger Band width dropped below the compression threshold "
        "(bands unusually narrow relative to their recent average). ATR is well below "
        "its rolling baseline. The HMM detected a very-low-variance return state in "
        "recent bars. Ensemble: 60% HMM + 40% rule-based. Source: BTC/USDT 1h, 200 bars."
    ),
    "uncertain": (
        "Detection path: No single regime met its confidence threshold. ADX is borderline "
        "(near 20–25), EMAs lack a clean cross, and the HMM probability distribution is "
        "spread across multiple states without a dominant winner. The ensemble could not "
        "reach a minimum confidence threshold — rather than forcing a guess, it reports "
        "'uncertain'. This is the safest and most conservative output. Source: BTC/USDT "
        "1h, 200 bars (or insufficient bars on first startup)."
    ),
    "accumulation": (
        "Detection path: Rule-based confirmed ADX < 20 (no trend established) + volume "
        "rising while price is flat or only mildly upward + RSI in the 30–55 zone. The "
        "HMM classified this as a low-volatility, positive-drift state. The divergence "
        "between volume increase and price stability is the key signal. Ensemble: 60% "
        "HMM + 40% rule-based. Source: BTC/USDT 1h, 200 bars."
    ),
    "distribution": (
        "Detection path: Rule-based confirmed price near 20-bar high + RSI elevated (55–75) "
        "+ volume declining on recent up-candles. The HMM classified this as a low-volatility "
        "state with slowing upward drift. The falling volume at high RSI with price near "
        "highs is the key divergence signal. Ensemble: 60% HMM + 40% rule-based. "
        "Source: BTC/USDT 1h, 200 bars."
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

        # Use a scroll area so the expanded text doesn't squash the layout
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background:transparent; border:none; }")

        inner = QWidget()
        v = QVBoxLayout(inner)
        v.setContentsMargins(12, 8, 12, 8)
        v.setSpacing(10)

        # ── What this regime means ──────────────────────────
        v.addWidget(_sep("WHAT THIS REGIME MEANS"))
        self._desc_lbl = QLabel(
            "No regime detected yet. The system needs at least 30 bars of indicator "
            "data to classify the current market state."
        )
        self._desc_lbl.setWordWrap(True)
        self._desc_lbl.setStyleSheet(f"color:{_LIGHT}; font-size:13px;")
        v.addWidget(self._desc_lbl)

        # ── How NexusTrader detected it ──────────────────────
        v.addWidget(_sep("HOW NEXUS TRADER DETECTED THIS REGIME"))
        self._detection_lbl = QLabel("Waiting for first classification…")
        self._detection_lbl.setWordWrap(True)
        self._detection_lbl.setStyleSheet(f"color:#9BB4CC; font-size:13px;")
        v.addWidget(self._detection_lbl)

        # ── Active strategies ───────────────────────────────
        v.addWidget(_sep("ACTIVE STRATEGIES IN THIS REGIME"))
        self._strategy_hint_lbl = QLabel("—")
        self._strategy_hint_lbl.setWordWrap(True)
        self._strategy_hint_lbl.setStyleSheet(f"color:{_YELLOW}; font-size:13px;")
        v.addWidget(self._strategy_hint_lbl)

        # ── Risk adjustment ─────────────────────────────────
        v.addWidget(_sep("RISK ADJUSTMENT"))
        self._risk_hint_lbl = QLabel("—")
        self._risk_hint_lbl.setWordWrap(True)
        self._risk_hint_lbl.setStyleSheet(f"color:{_LIGHT}; font-size:13px;")
        v.addWidget(self._risk_hint_lbl)

        v.addStretch()
        scroll.setWidget(inner)

        outer_v = QVBoxLayout(box)
        outer_v.setContentsMargins(0, 8, 0, 4)
        outer_v.addWidget(scroll, 1)
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

        # How-detected explanation
        self._detection_lbl.setText(
            _REGIME_DETECTION.get(regime,
                "Classification used BTC/USDT 1h data from Binance public API (no auth "
                "required). The ensemble combines a Hidden Markov Model (60% weight) with "
                "a rule-based indicator check (40% weight). Confidence reflects the degree "
                "of agreement between the two methods and the probability mass the HMM "
                "assigns to the winning state.")
        )

        strategy_hints = {
            "bull_trend": (
                "TrendModel (bull affinity=1.0) and MomentumBreakout (affinity=1.0) are "
                "fully active and fire at maximum weight. Disabled models — MeanReversion "
                "and LiquiditySweep — remain off. Long (BUY) signals are accepted; short "
                "signals require very high confluence. The EV gate applies a standard win "
                "probability to all signals. The multi-timeframe filter (if enabled) checks "
                "the 4h chart for agreement before approving a 1h-timeframe signal."
            ),
            "bear_trend": (
                "TrendModel is active and fires SELL signals at affinity=0.9 (90% of its "
                "normal weight). MomentumBreakout fires short signals at affinity=0.7. "
                "Disabled models — MeanReversion and LiquiditySweep — remain off. New "
                "LONG (BUY) entries are strongly filtered: the EV gate applies a -15% "
                "win-probability penalty to any long signal generated in a bear regime, "
                "effectively raising the confluence bar for longs. The multi-timeframe "
                "filter (if enabled) will block BUY signals where the 4h chart also shows "
                "a bear regime. Shorts and hedging positions can still be opened normally."
            ),
            "ranging": (
                "MeanReversion (affinity=1.0) and VWAPReversion (affinity=0.8) are the "
                "primary models in ranging conditions. TrendModel has low affinity (0.1) "
                "and will rarely fire — trending signals have elevated false-positive rates "
                "in sideways markets. MomentumBreakout's affinity drops to 0.2. The "
                "confluence threshold may lower slightly in ranging conditions, reflecting "
                "the higher frequency of reversion opportunities. Note: MeanReversion and "
                "LiquiditySweep are currently disabled via config — so only the reduced "
                "TrendModel and MomentumBreakout signals are active in this regime."
            ),
            "volatility_expansion": (
                "MomentumBreakout (vol_expansion affinity=1.0) is the primary active "
                "model. TrendModel fires at 0.6 affinity. ATR multipliers on stop-loss "
                "and take-profit calculations are automatically widened to account for "
                "the elevated true range — a stop placed too tightly will get hit on "
                "normal noise. Slippage risk is above average due to fast-moving markets. "
                "Position sizing is NOT automatically reduced but the wider stops mean "
                "each trade naturally risks more nominal distance from entry to stop."
            ),
            "volatility_compression": (
                "No trend-following models should be entered before the breakout direction "
                "is confirmed. The scanner will still run and evaluate signals, but a "
                "compression regime is a warning sign: signals that fire in compression "
                "often resolve against the position when the breakout occurs. If a signal "
                "does reach the approval threshold, stop-loss placement should be wide "
                "enough to survive the initial volatility of the breakout. Wait for a "
                "confirmed candle close beyond the compression zone before committing."
            ),
            "uncertain": (
                "All models fire at reduced affinity weights (e.g. TrendModel=0.3, "
                "MomentumBreakout=0.3, VWAPReversion=0.4). The dynamic confluence "
                "threshold rises to reflect the regime uncertainty — more signal agreement "
                "is required before a candidate is approved. The EV gate applies an "
                "additional -15% win-probability penalty to all signals in the uncertain "
                "regime. In practice, very few candidates are approved during this regime "
                "unless signals are unusually strong and consistent."
            ),
            "accumulation": (
                "Cautious long (BUY) accumulation is favoured. TrendModel fires longs at "
                "reduced affinity. The ideal entry is a quiet pullback toward support on "
                "low volatility — not a chase. Stops should be placed below recent "
                "support or the entry range. Take partial profits quickly; the move to "
                "bull_trend is not guaranteed and the accumulation phase can be long."
            ),
            "distribution": (
                "Reduce long exposure. New BUY signals should only be acted on if "
                "confluence is very high and the signal comes after a meaningful pullback. "
                "The system may approve SELL (short) signals if a confirmation of reversal "
                "appears (e.g. a high-volume down-candle breaks recent support). Watch "
                "closely for signs of regime transition to bear_trend — if that transition "
                "is confirmed, short positions become the primary strategy."
            ),
        }
        risk_hints = {
            "bull_trend": (
                "Standard position sizing applies — Quarter-Kelly formula with a 4% hard "
                "cap per trade. The CrashDetector's defensive_mode_multiplier is 1.0 "
                "(no reduction). The portfolio heat check limits total open risk to 6% of "
                "capital at any time. ATR multipliers are at their baseline values for "
                "stop-loss (1.5×) and take-profit (2.5×)."
            ),
            "bear_trend": (
                "Long (BUY) positions face a -15% win-probability reduction from the EV "
                "gate — they must show higher confluence to pass. Short positions use "
                "standard sizing. ATR stop multipliers are unchanged (bear_trend does not "
                "itself increase volatility enough to require wider stops). The portfolio "
                "heat check still enforces the 6% cap. If the CrashDetector escalates "
                "to DEFENSIVE or higher tiers, the defensive_mode_multiplier will "
                "proportionally reduce long-side position sizes. Monitor the Dashboard "
                "crash risk score — anything above 6.0 triggers size reduction."
            ),
            "ranging": (
                "ATR multipliers are reduced for MeanReversion entries (stop = 1.0× ATR "
                "for tighter risk, target = 1.5× ATR for more conservative exits). "
                "TrendModel-generated trades use wider multipliers to absorb oscillation. "
                "Standard Quarter-Kelly sizing and 4% hard cap apply. The reduced affinity "
                "of TrendModel means fewer trades fire in ranging conditions, keeping "
                "overall capital exposure lower than in trending regimes."
            ),
            "volatility_expansion": (
                "ATR multipliers are increased across all active sub-models to prevent "
                "premature stop-outs from the elevated true range. Expect wider stop-loss "
                "distances — this means each trade carries a larger nominal risk distance, "
                "even if the percentage risk per trade (Quarter-Kelly cap) is unchanged. "
                "Consider manually verifying that the Est. Size on approved candidates "
                "is not outsized before auto-execution fires."
            ),
            "volatility_compression": (
                "50% of normal position size is the recommended approach — uncertainty "
                "about breakout direction is the primary concern. The system will not "
                "automatically halve sizes (this would require a configuration change "
                "to risk_pct_per_trade), but fewer signals should be approved in this "
                "regime. Avoid layering multiple positions while compression persists."
            ),
            "uncertain": (
                "50% of normal position size is recommended. The regime uncertainty "
                "penalty and elevated confluence threshold collectively ensure very few "
                "trades are approved. If trades do fire, they have passed a stricter "
                "filter and represent the system's highest-confidence signals under "
                "ambiguous conditions. Standard 4% hard cap and 6% heat check remain "
                "active as normal safety nets."
            ),
            "accumulation": (
                "75% of normal position size is recommended — the move is not yet "
                "confirmed. Stop-losses should be placed slightly wider than normal to "
                "accommodate the low-volatility environment where noise can trigger tight "
                "stops. Targets should be modest on the first entry; if the regime "
                "transitions to bull_trend, the full position can be built up."
            ),
            "distribution": (
                "50% of normal position size. Tighten stops on existing long positions "
                "to lock in gains made during the preceding bull phase. Be alert for "
                "the regime transitioning to bear_trend — at that point, the risk "
                "adjustment shifts to the bear_trend profile above. New longs should be "
                "the exception, not the rule, during distribution."
            ),
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
