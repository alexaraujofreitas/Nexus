# ============================================================
# NEXUS TRADER — Help Center  (Phase 12)
#
# Searchable help center page with:
#   - Search bar filtering all help articles
#   - Category navigation (sidebar)
#   - Article reader panel
#   - Guided onboarding checklist for new users
# ============================================================
from __future__ import annotations

import logging
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QLineEdit, QListWidget, QListWidgetItem, QTextBrowser,
    QPushButton, QScrollArea, QGroupBox, QSplitter,
)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QColor

from gui.main_window import PageHeader

logger = logging.getLogger(__name__)

_GREEN  = "#00FF88"
_BLUE   = "#4299E1"
_GRAY   = "#4A5568"
_LIGHT  = "#C8D0E0"
_BG     = "#0D1B2A"

# ── Help article database ─────────────────────────────────────
# Each article: (category, title, content_markdown)

_ARTICLES: list[dict] = [
    # ── Getting Started ────────────────────────────────────────
    {
        "category": "Getting Started",
        "title":    "Welcome to NexusTrader",
        "content":  """## Welcome to NexusTrader

NexusTrader is an institutional-grade AI trading platform built for systematic traders.

### Core Philosophy
NexusTrader uses an Intelligent Decision Support System (IDSS) that analyses markets
across multiple dimensions before recommending any trade:

1. **Technical signals** — trend, mean reversion, momentum, liquidity
2. **Market microstructure** — order book pressure, funding rates, options flow
3. **Macro intelligence** — DXY, fear/greed, sector rotation, geopolitical risk
4. **AI sentiment** — news NLP, social media, on-chain metrics

All signals are combined by the Confluence Scorer and validated by the Risk Gate
before any trade is executed.

### First Steps
1. Go to **Exchange Management** and connect your exchange
2. Set your risk parameters in **Settings → Risk Management**
3. Explore strategies in the **Strategy Library**
4. Run a **Backtest** to validate a strategy before live trading
5. Start with **Paper Trading** to test in real market conditions
6. Enable **Live Trading** when you're confident

### Navigation
Use the left sidebar to navigate between modules. Each module has a **?** help button
that explains what it does and how it works.
""",
    },
    {
        "category": "Getting Started",
        "title":    "Exchange Setup",
        "content":  """## Exchange Setup

### Connecting Your Exchange
Go to **Exchange Management** in the sidebar and:
1. Select your exchange (Binance, Bybit, OKX, Coinbase, etc.)
2. Enter your API Key and Secret (get these from your exchange)
3. Choose Paper/Testnet mode first for safety
4. Click **Connect**

### API Key Requirements
- **Paper Trading**: Read-only key is sufficient
- **Live Trading**: Key must have **Spot/Futures Trading** permission enabled
- Restrict API key to your IP address for security

### Supported Exchanges
NexusTrader uses CCXT, so any exchange supported by CCXT will work.
Popular choices: Binance, Bybit, OKX, Kraken, Coinbase Pro.

### Security Notes
- API keys are encrypted in the local key vault (never stored in plain text)
- Never share your API keys
- Use separate keys per application
- Enable withdrawal restrictions if available
""",
    },
    # ── Trading Strategy ───────────────────────────────────────
    {
        "category": "Trading Strategy",
        "title":    "Understanding the IDSS Pipeline",
        "content":  """## The IDSS Signal Pipeline

The Intelligent Decision Support System (IDSS) processes each asset through a pipeline:

### Stage 1: Indicator Library
Calculates technical indicators from OHLCV data:
EMA, BB, ADX, RSI, ATR, Volume MA, and more.

### Stage 2: Regime Classifier
Determines the current market regime:
- Bull Trend / Bear Trend / Ranging
- Volatility Expansion / Compression
- Uncertain

### Stage 3: Signal Generator
Runs all sub-models. Each model independently evaluates:
- Should I fire? (conditions met)
- Direction (long or short)
- Strength (0.0–1.0)
- Entry, Stop Loss, Take Profit (ATR-based)

### Stage 4: Confluence Scorer
Combines all sub-model signals into one score.
Adjusts thresholds based on the Orchestrator meta-signal.
**Score ≥ threshold → proceed to Risk Gate**

### Stage 5: Risk Gate
Final safety checks:
- Max open positions
- Min risk-reward ratio
- Portfolio drawdown
- Macro veto state

### Stage 6: Order Router
Routes approved orders to:
- Paper Executor (paper mode)
- Live Executor (live mode)
""",
    },
    {
        "category": "Trading Strategy",
        "title":    "Strategy Sub-Models Explained",
        "content":  """## Strategy Sub-Models

### Trend Model
Fires on strong directional trends confirmed by:
- ADX > 25 (strong trend)
- EMA crossover confirmation
- Active in bull_trend and bear_trend regimes

### Mean Reversion Model
Fires when price has moved far from its mean:
- RSI oversold (<30) for longs
- RSI overbought (>70) for shorts
- Active in ranging and vol_compression regimes

### Momentum Breakout Model
Detects breakouts from consolidation:
- Price closes above/below Bollinger Bands
- Volume confirmation required
- Active in vol_expansion regime

### Liquidity Sweep Model
Identifies institutional stop-hunt moves:
- Wick through key support/resistance
- Price quickly recovers
- Potential for sharp reversals

### Funding Rate Model
Contrarian signal based on extreme funding:
- Very positive funding → crowded longs → short signal
- Very negative funding → crowded shorts → long signal

### Order Book Model
Uses live order book microstructure:
- Bid/ask imbalance > 70% → directional signal
- Large limit walls as support/resistance

### Sentiment Model
Uses AI news analysis:
- FinBERT NLP on recent headlines
- Fires on strong sentiment with confidence > 0.50
""",
    },
    # ── Risk Management ────────────────────────────────────────
    {
        "category": "Risk Management",
        "title":    "Risk Gate Settings",
        "content":  """## Risk Gate Configuration

Access in **Settings → Risk Management**

### Key Parameters

**Max Concurrent Positions** (default: 3)
Maximum number of simultaneously open positions.
Lower = less exposure, slower growth potential.

**Min Risk-Reward Ratio** (default: 1.3)
Every trade must have at least 1.3R potential return per 1R risk.
Signals with TP/SL ratio below this are rejected.

**Max Portfolio Drawdown** (default: 15%)
Emergency stop triggers if portfolio drops more than this % from peak.

**Max Position Size** (default: 2% of portfolio per trade)
Conservative default limits individual trade risk.

**Max Spread** (default: 0.3%)
Rejects trades when bid-ask spread is too wide (slippage too high).

### Recommendations
For new traders / small accounts:
- Max positions: 2-3
- Risk per trade: 1-2%
- Max drawdown: 10-15%

For experienced traders:
- These can be adjusted as you gain confidence in the strategies
- Always backtest with these settings applied
""",
    },
    {
        "category": "Risk Management",
        "title":    "Emergency Stop",
        "content":  """## Emergency Stop

The Emergency Stop immediately closes all open positions and halts all trading.

### Automatic Triggers
The system automatically activates the emergency stop when:
- Portfolio drawdown exceeds the configured maximum
- Exchange connection errors exceed the threshold
- Manual activation by user

### What Happens When Triggered
1. All pending orders are cancelled
2. All open positions are closed at market price
3. All new signal processing is paused
4. A critical notification is sent to all configured channels
5. The system logs the event with full context

### Restarting After Emergency Stop
1. Investigate the cause in the Logs page
2. Verify your positions are closed on the exchange
3. Review your risk settings
4. Restart market feeds and scanner manually

### Manual Activation
Use the **Emergency Stop** button in the Risk Management page.
This cannot be undone automatically — you must manually restart trading.
""",
    },
    # ── Intelligence Agents ────────────────────────────────────
    {
        "category": "Intelligence Agents",
        "title":    "Overview of Intelligence Agents",
        "content":  """## Intelligence Agents

NexusTrader has 8 intelligence agents that continuously monitor the market
and provide non-technical signals to the Orchestrator Engine.

### Agent Hierarchy (by weight)
1. **Funding Rate Agent** (25%) — perpetual futures positioning
2. **Order Book Agent** (22%) — microstructure and institutional flow
3. **Options Flow Agent** (18%) — smart money positioning (Deribit)
4. **Macro Agent** (17%) — macro environment (DXY, yields, fear/greed)
5. **Social Sentiment** (8%) — on-chain + social media signals
6. **News Agent** (5%) — AI NLP on crypto headlines
7. **Geopolitical Agent** (3%) — regulatory and geopolitical risk
8. **Sector Rotation** (2%) — ETF rotation flows (QQQ, GLD, etc.)

### Orchestrator Meta-Signal
The Orchestrator combines all 8 agents into a single meta-signal (-1 to +1).
This influences the Confluence Scorer's threshold:
- Bullish meta → lower threshold (easier to enter)
- Bearish meta → higher threshold (harder to enter)
- Extreme risk → Macro Veto blocks all trading

### Viewing Agent Status
Go to the **AI Intelligence Dashboard** to see all agent signals,
the orchestrator meta-signal, and WHY the system is currently bullish or bearish.
""",
    },
    {
        "category": "Intelligence Agents",
        "title":    "Configuring API Keys for Agents",
        "content":  """## Agent API Keys

Some agents require optional API keys for enhanced data:

### FRED API Key (Macro Agent)
- Free at fred.stlouisfed.org
- Provides: US economic indicators, treasury yields
- Without key: agent uses yfinance data (slightly delayed)

### LunarCrush API Key (Social Sentiment Agent)
- Free tier at lunarcrush.com
- Provides: detailed social volume metrics per coin
- Without key: agent uses CoinGecko + Fear & Greed Index

### NewsAPI Key (News Agent)
- Free tier at newsapi.org (100 requests/day)
- Provides: news from 80,000+ sources
- Without key: agent uses CryptoCompare headlines only

### Setting API Keys
Go to **Settings → Intelligence Agents** and enter your API keys.
All keys are stored in the encrypted local vault — never in plain text.
""",
    },
    # ── Backtesting ────────────────────────────────────────────
    {
        "category": "Backtesting",
        "title":    "How to Backtest a Strategy",
        "content":  """## Running a Backtest

### Quick Start
1. Go to the **Backtesting** page
2. Select a symbol (e.g. BTC/USDT)
3. Select a timeframe (1h recommended to start)
4. Set your date range (minimum 6 months recommended)
5. Configure your strategy parameters
6. Click **Run Backtest**

### Understanding Results

**Total Return** — What % would you have made over the period?

**Sharpe Ratio** — Risk-adjusted return. Above 1.0 is good.
Formula: (return - risk_free_rate) / standard_deviation

**Max Drawdown** — The worst peak-to-trough loss. Indicates worst-case pain.

**Win Rate** — % of trades that were profitable.
Note: high win rate doesn't mean profitable if losses are larger than wins.

**Profit Factor** — Gross profit / gross loss. Above 1.5 is healthy.

### Monte Carlo Simulation
Runs 1000 equity curve simulations using bootstrap resampling.
Shows the range of possible outcomes — not just the best case.
Look at P(profit) and P(ruin) percentages.

### Walk-Forward Validation
Validates on data the strategy has never "seen".
This detects overfitting. A strategy that only works on in-sample
data but not out-of-sample is overfit and not trustworthy.

### Important Warning
Past performance does not guarantee future results.
Always validate with walk-forward testing and run paper trading
for at least 2-4 weeks before going live.
""",
    },
    # ── Notifications ──────────────────────────────────────────
    {
        "category": "Notifications",
        "title":    "Setting Up WhatsApp Notifications",
        "content":  """## WhatsApp Notification Setup

### Requirements
- A Twilio account (twilio.com) — free trial available
- A WhatsApp-enabled phone number

### Step 1: Create Twilio Account
1. Sign up at twilio.com
2. Note your **Account SID** and **Auth Token** from the dashboard

### Step 2: Set Up WhatsApp Sandbox (Free Testing)
1. In Twilio Console → Messaging → Try it out → Send a WhatsApp message
2. Follow the instructions to join the sandbox from your phone
3. The sandbox from number is: **whatsapp:+14155238886**

### Step 3: Configure in NexusTrader
1. Go to **Settings → Notifications**
2. Enable WhatsApp
3. Enter your Account SID and Auth Token
4. From number: whatsapp:+14155238886 (sandbox) or your Twilio WhatsApp number
5. To number: whatsapp:+1YOURNUMBER (your WhatsApp number in E.164 format)
6. Click **Test WhatsApp** to verify

### For Production (Non-Sandbox)
You'll need to apply for a WhatsApp Business account through Twilio.
The sandbox is sufficient for personal use.
""",
    },
    {
        "category": "Notifications",
        "title":    "Setting Up Telegram Notifications",
        "content":  """## Telegram Notification Setup

### Creating a Telegram Bot
1. Open Telegram and search for **@BotFather**
2. Send the message: /newbot
3. Follow prompts to name your bot
4. BotFather will give you a **Bot Token** (save this)

### Getting Your Chat ID
Method 1: Search for **@userinfobot** on Telegram, start it, it shows your ID.
Method 2: After creating your bot, message it, then visit:
https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
Your chat_id is in the "from" → "id" field.

### Configure in NexusTrader
1. Go to **Settings → Notifications**
2. Enable Telegram
3. Enter your Bot Token
4. Enter your Chat ID
5. Click **Test Telegram** to verify

### Important Notes
- You must have messaged your bot first before it can send you messages
- For group chat notifications: add the bot to the group, use the group's chat ID
- Telegram notifications support Markdown formatting (bold, monospace code blocks)
""",
    },
]

# Build search index
_SEARCH_INDEX: list[tuple[int, str]] = []  # (article_idx, searchable_text)
for _i, _a in enumerate(_ARTICLES):
    _SEARCH_INDEX.append((_i, f"{_a['title']} {_a['category']} {_a['content']}".lower()))


class HelpCenterPage(QWidget):
    """
    Searchable help center with all NexusTrader documentation.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_article_idx = 0
        self._filtered_indices: list[int] = list(range(len(_ARTICLES)))
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(PageHeader(
            "Help Center",
            "Documentation, guides, and explanations for every NexusTrader module"
        ))

        # Search bar
        root.addWidget(self._build_search_bar())

        # Splitter: article list | content
        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(4)

        # Left: article list
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(12, 8, 4, 12)
        lv.setSpacing(8)

        list_box = QGroupBox("Articles")
        list_box.setStyleSheet(
            "QGroupBox { color:#C8D0E0; font-weight:700; font-size:13px;"
            " border:1px solid #1E3A5F; border-radius:6px;"
            " margin-top:8px; padding-top:12px; }"
        )
        lbv = QVBoxLayout(list_box)
        self._article_list = QListWidget()
        self._article_list.setStyleSheet(
            "QListWidget { background:#0D1B2A; border:none; color:#C8D0E0; font-size:13px; }"
            "QListWidget::item { padding:6px 8px; border-bottom:1px solid #1E2A3A; }"
            "QListWidget::item:selected { background:#1E3A5F; color:#00FF88; }"
            "QListWidget::item:hover { background:#0F2035; }"
        )
        self._article_list.currentRowChanged.connect(self._on_article_selected)
        lbv.addWidget(self._article_list)
        lv.addWidget(list_box)
        left.setMaximumWidth(260)
        splitter.addWidget(left)

        # Right: article reader
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(4, 8, 12, 12)
        rv.setSpacing(8)

        content_box = QGroupBox("Content")
        content_box.setStyleSheet(
            "QGroupBox { color:#C8D0E0; font-weight:700; font-size:13px;"
            " border:1px solid #1E3A5F; border-radius:6px;"
            " margin-top:8px; padding-top:12px; }"
        )
        cbv = QVBoxLayout(content_box)
        self._content_browser = QTextBrowser()
        self._content_browser.setOpenExternalLinks(True)
        self._content_browser.setStyleSheet(
            "QTextBrowser { background:#0A0E1A; color:#C8D0E0;"
            " border:none; font-size:13px; padding:8px; }"
            "QScrollBar:vertical { background:#0D1B2A; width:6px; border-radius:3px; }"
            "QScrollBar::handle:vertical { background:#1E3A5F; border-radius:3px; }"
        )
        cbv.addWidget(self._content_browser)
        rv.addWidget(content_box, 1)
        splitter.addWidget(right)
        splitter.setSizes([240, 640])

        root.addWidget(splitter, 1)

        # Populate list
        self._populate_list(list(range(len(_ARTICLES))))
        if _ARTICLES:
            self._article_list.setCurrentRow(0)
            self._show_article(0)

    def _build_search_bar(self) -> QFrame:
        bar = QFrame()
        bar.setFixedHeight(50)
        bar.setStyleSheet("background:#0D1B2A; border-bottom:1px solid #1E3A5F;")
        h = QHBoxLayout(bar)
        h.setContentsMargins(16, 8, 16, 8)
        h.setSpacing(12)

        h.addWidget(QLabel("🔍"))

        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText(
            "Search help articles… (e.g. 'backtest', 'confluence', 'funding rate')"
        )
        self._search_input.textChanged.connect(self._on_search)
        self._search_input.setStyleSheet(
            "background:#1A2535; border:1px solid #2A3A50; border-radius:4px;"
            " padding:4px 8px; color:#C8D0E0; font-size:13px;"
        )
        h.addWidget(self._search_input, 1)

        clear_btn = QPushButton("Clear")
        clear_btn.setFixedWidth(60)
        clear_btn.clicked.connect(self._clear_search)
        h.addWidget(clear_btn)

        self._result_count_lbl = QLabel(f"{len(_ARTICLES)} articles")
        self._result_count_lbl.setStyleSheet(f"color:{_GRAY}; font-size:13px;")
        h.addWidget(self._result_count_lbl)

        return bar

    # ── Search ────────────────────────────────────────────────

    def _on_search(self, query: str) -> None:
        query = query.strip().lower()
        if not query:
            indices = list(range(len(_ARTICLES)))
        else:
            terms = query.split()
            indices = [
                idx for idx, text in _SEARCH_INDEX
                if all(term in text for term in terms)
            ]

        self._filtered_indices = indices
        self._populate_list(indices)
        self._result_count_lbl.setText(
            f"{len(indices)} result{'s' if len(indices) != 1 else ''}"
        )

        if indices:
            self._article_list.setCurrentRow(0)
            self._show_article(indices[0])

    def _clear_search(self) -> None:
        self._search_input.clear()

    # ── Article display ───────────────────────────────────────

    def _populate_list(self, indices: list[int]) -> None:
        self._article_list.clear()
        current_cat = None
        for idx in indices:
            article = _ARTICLES[idx]
            if article["category"] != current_cat:
                current_cat = article["category"]
                cat_item = QListWidgetItem(f"  {current_cat.upper()}")
                cat_item.setForeground(QColor(_GRAY))
                cat_item.setFlags(Qt.ItemIsEnabled)  # not selectable
                font = cat_item.font()
                font.setBold(True)
                font.setPointSize(8)
                cat_item.setFont(font)
                self._article_list.addItem(cat_item)

            item = QListWidgetItem(f"    {article['title']}")
            item.setData(Qt.UserRole, idx)
            item.setForeground(QColor(_LIGHT))
            self._article_list.addItem(item)

    def _on_article_selected(self, row: int) -> None:
        item = self._article_list.item(row)
        if item is None:
            return
        idx = item.data(Qt.UserRole)
        if idx is not None:
            self._show_article(idx)

    def _show_article(self, idx: int) -> None:
        if idx >= len(_ARTICLES):
            return
        article = _ARTICLES[idx]
        content = article["content"]

        # Convert markdown to basic HTML
        html = self._md_to_html(content)
        self._content_browser.setHtml(html)

    @staticmethod
    def _md_to_html(text: str) -> str:
        """Convert basic markdown to HTML for QTextBrowser."""
        lines = text.split("\n")
        html_lines = []
        in_list = False

        for line in lines:
            if line.startswith("## "):
                if in_list:
                    html_lines.append("</ul>")
                    in_list = False
                html_lines.append(
                    f"<h2 style='color:#C8D0E0; border-bottom:1px solid #1E3A5F;"
                    f" padding-bottom:4px; margin-top:16px;'>{line[3:]}</h2>"
                )
            elif line.startswith("### "):
                if in_list:
                    html_lines.append("</ul>")
                    in_list = False
                html_lines.append(
                    f"<h3 style='color:#4299E1; margin-top:12px;'>{line[4:]}</h3>"
                )
            elif line.startswith("- ") or line.startswith("* "):
                if not in_list:
                    html_lines.append("<ul style='margin:4px 0; padding-left:20px;'>")
                    in_list = True
                item_text = line[2:]
                # Bold **text**
                item_text = _bold(item_text)
                html_lines.append(
                    f"<li style='color:#C8D0E0; margin:2px 0;'>{item_text}</li>"
                )
            elif line.startswith("1. ") or (len(line) > 2 and line[0].isdigit() and line[1] == "."):
                if in_list:
                    html_lines.append("</ul>")
                    in_list = False
                text_part = line[line.index(". ")+2:]
                text_part = _bold(text_part)
                html_lines.append(
                    f"<p style='color:#C8D0E0; margin:3px 0;'>&nbsp;&nbsp;{line[:line.index('.')+1]} {text_part}</p>"
                )
            elif line.strip() == "":
                if in_list:
                    html_lines.append("</ul>")
                    in_list = False
                html_lines.append("<br>")
            else:
                if in_list:
                    html_lines.append("</ul>")
                    in_list = False
                line = _bold(line)
                html_lines.append(f"<p style='color:#C8D0E0; margin:3px 0;'>{line}</p>")

        if in_list:
            html_lines.append("</ul>")

        body = "\n".join(html_lines)
        return (
            "<html><body style='font-family:sans-serif; background:#0A0E1A;"
            " color:#C8D0E0; padding:8px; line-height:160%;'>"
            + body
            + "</body></html>"
        )


def _bold(text: str) -> str:
    """Convert **text** to <b>text</b>."""
    import re
    return re.sub(r"\*\*(.+?)\*\*", r"<b style='color:#FFFFFF;'>\1</b>", text)
