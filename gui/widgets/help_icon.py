# ============================================================
# NEXUS TRADER — Help Icon Widget
#
# A small "?" button that opens a contextual help panel when clicked.
# Usage:
#   from gui.widgets.help_icon import HelpIcon
#   layout.addWidget(HelpIcon("idss_confluence_scorer"))
#
# The topic string maps to a section in the help registry.
# ============================================================
from __future__ import annotations

from PySide6.QtWidgets import QPushButton, QToolTip
from PySide6.QtCore import Qt, QPoint
from PySide6.QtGui import QFont, QCursor


# ── Help content registry ─────────────────────────────────────
# Maps help topic keys to (title, short_description, full_help_text)
_HELP_REGISTRY: dict[str, tuple[str, str, str]] = {
    # ── Core trading concepts ──────────────────────────────────
    "idss_confluence_scorer": (
        "Confluence Scorer",
        "Combines multiple sub-model signals into one weighted score",
        """The Confluence Scorer aggregates signals from all active sub-models
(Trend, Mean Reversion, Momentum Breakout, Liquidity Sweep, Funding Rate,
Order Book, Sentiment) into a single weighted score between 0.0 and 1.0.

A score above the minimum threshold (default 0.55) means enough sub-models
agree that a trade setup is valid. Higher scores mean stronger agreement.

Each sub-model has a weight:
  • Trend: 35%
  • Mean Reversion: 25%
  • Momentum Breakout: 25%
  • Liquidity Sweep: 15%
  • Funding Rate: 20%
  • Order Book: 18%
  • Sentiment: 12%

Only signals that pass both the confluence threshold AND the risk gate
will proceed to order execution.""",
    ),
    "idss_risk_gate": (
        "Risk Gate",
        "Final safety check before any order is placed",
        """The Risk Gate is the last line of defence before a trade is executed.
It checks:
  • Maximum concurrent open positions
  • Minimum risk-reward ratio (default 1.3:1)
  • Maximum portfolio drawdown
  • Position size limits
  • Maximum spread
  • Orchestrator macro veto state

If any check fails, the signal is rejected and logged as REJECTED.
Rejected signals are visible in the Signal Explorer page.""",
    ),
    "regime_classifier": (
        "Market Regime Classifier",
        "Identifies the current market condition (trend, range, volatile, etc.)",
        """The Regime Classifier analyses recent price action using three indicators:
  • ADX (Average Directional Index) — measures trend strength
  • EMA slope — determines trend direction
  • Bollinger Band width — detects volatility state

Six regimes are detected:
  1. Bull Trend     — strong upward trend (ADX > 25, EMA slope positive)
  2. Bear Trend     — strong downward trend (ADX > 25, EMA slope negative)
  3. Ranging        — low ADX market oscillating between levels
  4. Vol Expansion  — Bollinger Bands widening (breakout forming)
  5. Vol Compression— Bollinger Bands squeezing (coiling before move)
  6. Uncertain      — insufficient data or borderline readings

The HMM layer adds a probabilistic overlay: instead of hard classifications,
it gives a probability distribution across all 6 regimes.""",
    ),
    "hmm_regime_classifier": (
        "HMM Regime Classifier",
        "Hidden Markov Model adds probabilistic regime detection",
        """The Hidden Markov Model (HMM) Regime Classifier uses machine learning
to learn regime patterns from historical data (minimum 200 bars).

Unlike the rule-based classifier, HMM produces a probability distribution
across all 6 regimes — e.g. 60% bull_trend, 20% ranging, 20% uncertain.

The final classification is an ensemble:
  • HMM: 60% weight
  • Rule-based: 40% weight

Benefits:
  • Smoother transitions (less whipsaw)
  • Uncertainty quantification (when probs are split, system is more cautious)
  • Learns regime patterns specific to the asset being traded

If hmmlearn is not installed, the system falls back to rule-based only.""",
    ),
    "orchestrator_engine": (
        "Orchestrator Engine",
        "Combines all 8 intelligence agents into a meta-signal",
        """The Orchestrator Engine is the 'brain' of the intelligence layer.
It subscribes to all 8 agent signals and combines them into a single
meta-signal using weighted averaging.

Agent weights (by default):
  • Funding Rate Agent:    25%
  • Order Book Agent:      22%
  • Options Flow Agent:    18%
  • Macro Agent:           17%
  • Social Sentiment:       8%
  • News Agent:             5%
  • Geopolitical Agent:     3%
  • Sector Rotation:        2%

The meta-signal is used to:
  1. Boost confluence threshold when meta is positive (easier to trade)
  2. Raise confluence threshold when meta is negative (harder to trade)
  3. Activate macro veto (blocks ALL trades) when macro risk is very high

View the current meta-signal and all agent statuses on the
AI Intelligence Dashboard page.""",
    ),
    "signal_generator": (
        "Signal Generator",
        "Runs all sub-models and collects their trade signals",
        """The Signal Generator evaluates all sub-models against the current
market data for each scanned asset.

Sub-models produce a ModelSignal with:
  • direction (long/short)
  • strength (0.0–1.0)
  • entry_price
  • stop_loss (ATR-based)
  • take_profit (ATR-based)
  • rationale (text explanation)

Models only fire when their specific conditions are met — most models
produce no signal most of the time. When multiple models fire in the
same direction, the Confluence Scorer combines them.""",
    ),
    "backtesting": (
        "Backtesting Engine",
        "Test strategies on historical data before risking real capital",
        """The backtesting engine simulates strategy performance on historical
OHLCV data. It runs the full IDSS pipeline (signals, confluence scoring,
risk gate) on each historical bar.

Key metrics produced:
  • Total Return %
  • Sharpe Ratio (annualised)
  • Max Drawdown %
  • Win Rate %
  • Profit Factor
  • Total trades

Advanced features:
  • Monte Carlo Simulation: runs 1000 equity curve simulations by
    bootstrap resampling your trades to estimate real-world variance
  • Walk-Forward Validation: validates strategy on out-of-sample data
    using expanding windows to detect overfitting

Important: Past backtest results do NOT guarantee future performance.
Always run walk-forward validation before going live.""",
    ),
    "paper_trading": (
        "Paper Trading",
        "Test strategies in real-time without risking real money",
        """Paper Trading simulates trades in real-time using live market data
but does not submit any orders to the exchange.

The full IDSS pipeline runs exactly as in live mode — same signals,
same risk gate, same position sizing. The only difference is that
'executed' trades are simulated in the paper executor.

This is useful for:
  • Verifying a backtest hypothesis on real-time data
  • Testing execution logic before going live
  • Building confidence in a new strategy

Monitor paper trading performance in the Performance Analytics page.""",
    ),
    "news_sentiment": (
        "News & Sentiment",
        "AI-powered news and social media sentiment analysis",
        """The sentiment system uses two NLP models:
  1. FinBERT — a financial BERT model that classifies news headlines as
     positive, negative, or neutral with financial context awareness
  2. VADER — rule-based sentiment analysis (fast fallback)

Data sources:
  • NewsAPI (requires API key) — major crypto news outlets
  • CryptoCompare — free, no API key needed
  • Messari — free research headlines

Sentiment signals feed directly into the Sentiment sub-model which
can influence trade decisions when news sentiment is strong (|signal| > 0.35)
and confident (confidence > 0.50).""",
    ),
    "funding_rate": (
        "Funding Rate Agent",
        "Monitors perpetual futures funding rates as a contrarian signal",
        """Funding rates in perpetual futures markets are periodic payments
between long and short holders.

When funding is very positive (e.g. > 0.1%), longs are paying shorts
heavily → market is over-leveraged long → contrarian SELL signal.

When funding is very negative (e.g. < -0.05%), shorts are paying longs
heavily → market is over-leveraged short → contrarian BUY signal.

The agent fetches funding rates from the exchange API every 30 minutes.
Extreme rates are weighted higher in the Confluence Scorer as they
often precede sharp reversals.""",
    ),
    "order_book": (
        "Order Book Agent",
        "Analyses order book microstructure for institutional signals",
        """The Order Book Agent examines the live order book for:
  • Bid/Ask imbalance — if 70% of size is on bid side, buyers are dominant
  • Large walls — unusually large orders at specific price levels
  • Liquidation clusters — areas where stop-losses are likely concentrated

Interpretation:
  • High bid imbalance + large bid wall = bullish signal
  • High ask imbalance + large ask wall = bearish signal
  • Liquidation clusters above price = potential upside target
  • Liquidation clusters below price = potential downside risk

This agent is most useful for short-term entries, as order books
change rapidly.""",
    ),
    "macro_agent": (
        "Macro Intelligence Agent",
        "Monitors macro economic indicators that affect crypto",
        """The Macro Agent monitors global macro indicators that historically
correlate with crypto market direction:

  • Fear & Greed Index (alternative.me) — crypto market sentiment
  • DXY (US Dollar Index) — inverse correlation with crypto
  • US 10-Year Treasury Yield — rising yields = risk-off
  • SPX (S&P 500) — risk-on/risk-off indicator

The agent produces a macro_risk_score (0.0 = very bullish, 1.0 = very bearish)
and a regime_bias (risk_on / risk_off / neutral).

When macro_risk_score > 0.75 AND combined agent signals are very bearish,
the Orchestrator can activate a macro VETO that blocks all new trades.""",
    ),
    "notifications": (
        "Trade Notifications",
        "Receive real-time alerts via WhatsApp, Telegram, Email, or SMS",
        """NexusTrader can send real-time notifications to your phone or email
for all important trading events.

Supported channels:
  • WhatsApp (primary) — via Twilio Business API
  • Telegram — via Telegram Bot API (free)
  • Email — via SMTP (Gmail, Outlook, custom)
  • SMS — via Twilio SMS API

Notification types:
  • Trade opened / closed / stopped
  • Signal rejected (optional)
  • Risk warnings and drawdown alerts
  • Emergency stop activation
  • System errors
  • Daily summary (optional)

Configure channels in Settings → Notifications.""",
    ),
}


class HelpIcon(QPushButton):
    """
    A small "?" button that shows a tooltip or opens a help panel.

    Usage:
        from gui.widgets.help_icon import HelpIcon
        layout.addWidget(HelpIcon("regime_classifier"))
    """

    def __init__(self, topic: str, parent=None):
        super().__init__("?", parent)
        self._topic = topic

        self.setFixedSize(18, 18)
        self.setCursor(Qt.WhatsThisCursor)
        self.setObjectName("HelpIcon")
        self.setStyleSheet(
            "QPushButton#HelpIcon {"
            "  background: #1E3A5F; color: #4299E1;"
            "  border: 1px solid #4299E1; border-radius: 9px;"
            "  font-size: 13px; font-weight: 700;"
            "}"
            "QPushButton#HelpIcon:hover {"
            "  background: #4299E1; color: #0A0E1A;"
            "}"
        )
        self.setToolTip(self._get_tooltip())
        self.clicked.connect(self._on_clicked)

    def _get_tooltip(self) -> str:
        entry = _HELP_REGISTRY.get(self._topic)
        if entry:
            title, short, _ = entry
            return f"{title}: {short}"
        return f"Help: {self._topic}"

    def _on_clicked(self) -> None:
        """Open help panel or show detailed tooltip."""
        entry = _HELP_REGISTRY.get(self._topic)
        if not entry:
            QToolTip.showText(
                QCursor.pos(),
                f"No help available for topic: {self._topic}",
                self,
            )
            return

        title, short, full = entry
        # Try to open the help panel (if available in parent hierarchy)
        try:
            from gui.widgets.help_panel import HelpPanel
            panel = HelpPanel(title, full, parent=self.window())
            panel.show()
        except Exception:
            # Fallback to tooltip
            QToolTip.showText(QCursor.pos(), f"<b>{title}</b><br>{short}<br><br>{full[:300]}…", self)


def get_help_content(topic: str) -> tuple[str, str, str] | None:
    """Return (title, short, full) for a topic, or None if not found."""
    return _HELP_REGISTRY.get(topic)


def register_help(topic: str, title: str, short: str, full: str) -> None:
    """Register custom help content at runtime."""
    _HELP_REGISTRY[topic] = (title, short, full)
