# NexusTrader — Complete Build Specification

> **Purpose:** This document is the authoritative, self-contained specification for NexusTrader.
> Anyone (human developer or AI assistant) executing this specification from scratch should
> produce a functionally identical application.

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Technology Stack](#2-technology-stack)
3. [Project Structure](#3-project-structure)
4. [Design System & Theme](#4-design-system--theme)
5. [Database Schema](#5-database-schema)
6. [Configuration & Settings](#6-configuration--settings)
7. [Core Engine — Backtesting](#7-core-engine--backtesting)
8. [Core Engine — Indicator Library](#8-core-engine--indicator-library)
9. [Core Engine — AI Module](#9-core-engine--ai-module)
10. [Navigation & Main Window](#10-navigation--main-window)
11. [Page Specifications](#11-page-specifications)
    - [Dashboard](#111-dashboard-page)
    - [Market Scanner](#112-market-scanner-page)
    - [Chart Workspace](#113-chart-workspace-page)
    - [Rule Builder](#114-rule-builder-page)
    - [Strategies](#115-strategies-page)
    - [AI Strategy Lab](#116-ai-strategy-lab-page)
    - [Backtesting](#117-backtesting-page)
    - [Paper Trading](#118-paper-trading-page)
    - [News & Sentiment](#119-news--sentiment-page)
    - [Risk Management](#1110-risk-management-page)
    - [Orders & Positions](#1111-orders--positions-page)
    - [Performance Analytics](#1112-performance-analytics-page)
    - [Exchange Management](#1113-exchange-management-page)
    - [Logs](#1114-logs-page)
    - [Settings](#1115-settings-page)
12. [Shared Widgets](#12-shared-widgets)
13. [Event Bus](#13-event-bus)
14. [Behavioral Requirements](#14-behavioral-requirements)
15. [Build & Run Instructions](#15-build--run-instructions)

---

## 1. Project Overview

**Application Name:** Nexus Trader
**Version:** 1.0.0
**Description:** Institutional-Grade AI Trading Platform
**Paradigm:** Desktop GUI application (Bloomberg-style dark theme)
**Execution Model:** Python desktop app using PySide6; background operations via QThread workers

NexusTrader is a full-stack algorithmic trading platform designed for professional and institutional traders. It supports rule-based and AI-generated trading strategies, a complete 6-stage strategy lifecycle from generation to live trading, backtesting with realistic simulation, a market scanner powered by CoinGecko and technical indicators, an AI Strategy Lab powered by large language models (Claude / OpenAI), and comprehensive risk and performance management.

---

## 2. Technology Stack

| Layer | Library / Tool | Version |
|---|---|---|
| GUI Framework | PySide6 | ≥ 6.4 |
| Charting | pyqtgraph | latest |
| Database ORM | SQLAlchemy | ≥ 2.0 |
| Database Engine | SQLite | bundled |
| Exchange Connectivity | ccxt | latest |
| Technical Indicators | ta | latest (pandas-TA compatible) |
| HTTP Requests | requests | latest |
| Data Manipulation | pandas, numpy | latest |
| AI — Anthropic | anthropic | ≥ 0.7.0 |
| AI — OpenAI | openai | ≥ 1.0.0 |
| Configuration | PyYAML | latest |
| Numeric Helpers | scipy (optional) | latest |
| Python Version | 3.10+ | |

### requirements.txt
```
PySide6>=6.4
pyqtgraph
SQLAlchemy>=2.0
ccxt
ta
requests
pandas
numpy
anthropic>=0.7.0
openai>=1.0.0
PyYAML
scipy
```

---

## 3. Project Structure

```
NexusTrader/
├── main.py                          # Entry point — QApplication, splash, main window
├── config/
│   ├── constants.py                 # App-wide constants (no logic)
│   └── settings.py                  # YAML-backed AppSettings singleton
├── core/
│   ├── event_bus.py                 # Global publish/subscribe EventBus + Topics enum
│   ├── ai/
│   │   ├── __init__.py
│   │   ├── condition_parser.py      # NL text → RuleEvaluator tree translator
│   │   ├── llm_provider.py          # LLMProvider ABC + ClaudeProvider + OpenAIProvider
│   │   └── strategy_agent.py        # System prompt builder + proposal parser
│   ├── backtesting/
│   │   └── backtest_engine.py       # RuleEvaluator + run_backtest()
│   ├── database/
│   │   ├── engine.py                # SQLAlchemy engine, session factory, schema migration
│   │   └── models.py                # All ORM models + helper functions + constants
│   ├── features/
│   │   └── indicator_library.py     # calculate_all() — computes 30+ indicators
│   └── market_data/
│       ├── data_feed.py             # Real-time ticker feed (WebSocket or polling)
│       ├── exchange_manager.py      # CCXT exchange instance management
│       └── historical_loader.py     # OHLCV fetch + cache layer
├── gui/
│   ├── main_window.py               # MainWindow, Sidebar, PageHeader, StatusBar
│   ├── theme/
│   │   ├── theme_manager.py         # ThemeManager.apply_dark_theme()
│   │   └── dark_theme.qss           # Full Bloomberg-style stylesheet (~750 lines)
│   ├── pages/
│   │   ├── dashboard/dashboard_page.py
│   │   ├── market_scanner/scanner_page.py
│   │   ├── chart_workspace/chart_page.py
│   │   ├── rule_builder/rule_builder_page.py
│   │   ├── strategies/strategies_page.py
│   │   ├── ai_strategy_lab/ai_lab_page.py
│   │   ├── backtesting/backtesting_page.py
│   │   ├── paper_trading/paper_trading_page.py
│   │   ├── news_sentiment/news_sentiment_page.py
│   │   ├── risk_management/risk_page.py
│   │   ├── orders_positions/orders_page.py
│   │   ├── performance_analytics/analytics_page.py
│   │   ├── exchange_management/exchange_page.py
│   │   ├── logs/logs_page.py
│   │   └── settings/settings_page.py
│   └── widgets/
│       ├── chart_widget.py          # Candlestick + indicator chart widget (pyqtgraph)
│       └── chat_widget.py           # ChatBubble, StrategyProposalCard, ChatHistoryWidget, ChatInputBar
└── data/
    ├── cache/                       # Cached OHLCV data
    └── models/                      # Saved ML model files
```

---

## 4. Design System & Theme

### Philosophy
Bloomberg-style dark terminal. Every pixel should communicate data density, professionalism, and precision. No gradients, no rounded corners on panels, no decorative color.

### Color Palette

```python
# Backgrounds
BG_PRIMARY   = "#0A0E1A"   # deepest — app background, sidebar
BG_SECONDARY = "#0F1623"   # panels, inputs, table backgrounds
BG_PANEL     = "#131B2E"   # cards, group boxes
BG_CARD      = "#1A2332"   # elevated cards

# Accents
ORANGE       = "#FF6B00"   # primary CTA, active nav item border, focus rings
BLUE         = "#1E90FF"   # info, IF-block label, backtest primary
GREEN        = "#00FF88"   # bullish, success, live/active states
GREEN_DIM    = "#00CC77"   # secondary success
GREEN_DARK   = "#00AA55"   # hover on success buttons
RED          = "#FF3355"   # bearish, danger, errors
RED_DIM      = "#CC2244"   # hover on danger buttons
YELLOW       = "#FFD700"   # paper mode, warnings
ORANGE_WARN  = "#FFB300"   # system warnings
PURPLE       = "#AA44CC"   # AI-type strategy badge
TEAL         = "#008888"   # ensemble-type strategy badge
AMBER        = "#CC8800"   # ML-type strategy badge

# Text
TEXT_PRIMARY   = "#E8EBF0"   # all primary text
TEXT_SECONDARY = "#8899AA"   # labels, placeholders, muted info
TEXT_MUTED     = "#4A5568"   # disabled, very muted text

# Borders
BORDER_STD    = "#1E2D40"   # all standard borders
BORDER_ACTIVE = "#2D4A6B"   # focused/hovered borders

# Lifecycle Stage Colors
STAGE_1 = "#888888"   # Generation
STAGE_2 = "#4488CC"   # Backtesting
STAGE_3 = "#AA66CC"   # Walk-Forward
STAGE_4 = "#FF9800"   # Out-of-Sample
STAGE_5 = "#00AA88"   # Shadow Trading
STAGE_6 = "#00CC77"   # Live Trading
```

### Typography
- Font: System default (platform sans-serif); no custom font imports
- Page titles: 20px, bold, `#E8EBF0`
- Section headers: 13px, bold, `#8899AA`, uppercase
- Body text: 12-13px, `#E8EBF0`
- Metric values: 22-28px, bold, colored by context
- Labels: 11px, `#8899AA`

### Component Specifications

**Sidebar (fixed 220px wide):**
- Background: `#0F1623`
- Top logo: `"NEXUS TRADER  ▸  INSTITUTIONAL"` — bold white + dimmed gold
- Nav items: icon (15px) + label, 36px height, 8px left padding, 12px right padding
- Active nav item: `#FF6B00` 3px left border, `#1A2332` background
- Section headers: 9px uppercase, `#4A5568`, `#0A0E1A` background
- Bottom mode badge: colored `●` dot + text — Paper `#FFD700`, Live `#FF3355`, Shadow `#88AAFF`, Inactive `#4A5568`

**Page Header:**
- White separator line from sidebar to right edge
- Title (20px bold) + subtitle (12px `#8899AA`) on left
- Right-aligned slot for action buttons
- Bottom border: 1px `#1E2D40`

**Buttons:**
- Primary (orange): `#FF6B00` bg, white text, 6px radius, hover `#E05A00`
- Success (green): `#00AA55` bg, white text, hover `#008844`
- Danger (red): `#CC2244` bg, white text, hover `#AA1133`
- Ghost (transparent): transparent bg, `#8899AA` text, `#1E2D40` border, hover `#1A2332` bg
- All buttons: 36px height default, `PointingHandCursor`

**Inputs / Spinboxes / Comboboxes:**
- Background: `#0F1623`
- Border: 1px `#1E2D40`
- Focus border: 1px `#FF6B00`
- Text: `#E8EBF0`
- Placeholder: `#4A5568`
- Height: 32px
- Radius: 4px

**Tables (QTableWidget):**
- Background: `#0F1623`
- Header: `#0A0E1A` background, `#8899AA` text, bold
- Alternating rows: `#0F1623` / `#0C1018`
- Selection: `#1A2D45` background, no border
- Grid: 1px `#1E2D40`
- Row height: 32px
- No focus rectangle on cells

**Cards / Group Boxes:**
- Background: `#131B2E`
- Border: 1px `#1E2D40`
- Radius: 6px
- Title: bold, `#8899AA`

**Tab Widgets:**
- Background: `#0F1623`
- Active tab: underline `#FF6B00`, text `#FF6B00`
- Inactive tab: text `#8899AA`

**Alert Banners:**
- Warning: `#1A1400` bg, `#FFB300` border (4px left), `#FFB300` text
- Error: `#1A0010` bg, `#FF3355` border (4px left), `#FF3355` text
- Success: `#001A0E` bg, `#00CC77` border (4px left), `#00CC77` text
- Info: `#001020` bg, `#1E90FF` border (4px left), `#1E90FF` text

---

## 5. Database Schema

All models use SQLAlchemy 2.0 declarative syntax. Database: SQLite at `data/nexus_trader.db`. Schema migrations use `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` pattern in `engine._migrate_schema()`.

### Models

#### Exchange
```python
id, name, exchange_id (ccxt key), api_key_encrypted, api_secret_encrypted,
api_passphrase_encrypted, sandbox_mode (bool), testnet_url, is_active (bool),
created_at, updated_at
```

#### Asset
```python
id, symbol (e.g. "BTC/USDT"), base_currency, quote_currency,
exchange_id (FK→Exchange), min_order_size, price_precision, quantity_precision,
is_active (bool), last_synced_at
```

#### OHLCV
```python
id, asset_id (FK→Asset), timeframe, timestamp (UTC epoch ms),
open, high, low, close, volume
# Unique constraint: (asset_id, timeframe, timestamp)
```

#### Feature
```python
id, asset_id (FK→Asset), timeframe, timestamp,
feature_name, feature_value (Float)
```

#### SentimentData
```python
id, asset_id (FK→Asset), timestamp, source (news/reddit/twitter/onchain),
sentiment_score (-1.0 to 1.0), narrative_score, attention_index,
raw_data (JSON), created_at
```

#### Strategy
```python
id, name, type (rule/ai/ml/ensemble), status, lifecycle_stage (1–6),
description, definition (JSON), ai_generated (bool, default False),
backtest_result_id (FK→BacktestResult nullable),
created_at, updated_at
```

#### StrategyMetrics
```python
id, strategy_id (FK→Strategy), win_rate, profit_factor,
sharpe_ratio, sortino_ratio, calmar_ratio,
max_drawdown_pct, total_pnl, total_trades,
period_start, period_end, computed_at
```

#### TradingRule
```python
id, name, description, condition_tree (JSON),
status (draft/active/paused/completed/failed),
created_at, updated_at
```

#### Signal
```python
id, strategy_id (FK→Strategy), asset_id (FK→Asset),
timeframe, timestamp, signal_type (entry/exit),
direction (long/short), confidence (0.0–1.0),
regime, indicator_values (JSON), sentiment_score,
microstructure_score, created_at
```

#### Trade
```python
id, strategy_id (FK→Strategy), asset_id (FK→Asset),
mode (backtest/shadow/paper/live), direction (long/short),
entry_price, exit_price, quantity,
entry_time, exit_time, exit_reason,
gross_pnl, net_pnl, fee, slippage,
stop_loss_price, take_profit_price,
signal_id (FK→Signal nullable), created_at
```

#### Order
```python
id, trade_id (FK→Trade nullable), asset_id (FK→Asset),
exchange_order_id, order_type (market/limit/stop_limit/stop_market),
side (buy/sell), status, price, quantity,
filled, remaining, fee, created_at, updated_at
```

#### Position
```python
id, strategy_id (FK→Strategy), asset_id (FK→Asset),
mode, direction, entry_price, quantity,
current_price, unrealized_pnl,
stop_loss_price, take_profit_price,
opened_at, updated_at
```

#### MLModel
```python
id, name, version, model_type (lgbm/xgb/nn/ensemble),
target (direction/return/regime), asset_id (FK nullable),
timeframe, feature_importance (JSON), accuracy, precision_score,
recall_score, f1_score, train_start, train_end,
file_path, is_active (bool), created_at, updated_at
```

#### ModelPrediction
```python
id, model_id (FK→MLModel), asset_id (FK→Asset),
timeframe, timestamp,
bullish_probability, bearish_probability,
expected_return, confidence, features_used (JSON), created_at
```

#### BacktestResult
```python
id, strategy_id (FK→Strategy), asset_id (FK→Asset),
timeframe, start_date, end_date,
initial_capital, final_capital, total_return_pct,
annualized_return_pct, max_drawdown_pct, sharpe_ratio,
sortino_ratio, calmar_ratio, win_rate, profit_factor,
total_trades, winning_trades, losing_trades,
avg_trade_duration_hours, equity_curve (JSON), trade_log (JSON),
parameters (JSON), created_at
```

#### MarketRegime
```python
id, asset_id (FK→Asset), timeframe, timestamp,
regime (bull_trend/bear_trend/ranging/volatility_expansion/volatility_compression),
confidence (0.0–1.0), indicators (JSON), created_at
```

#### PortfolioSnapshot
```python
id, timestamp, mode, total_value, cash_balance,
positions_value, daily_pnl, total_pnl,
holdings (JSON), created_at
```

#### AIConversation
```python
id, title, strategy_id (FK→Strategy nullable),
created_at, updated_at
# relationship: messages → [AIMessage]
```

#### AIMessage
```python
id, conversation_id (FK→AIConversation), role (user/assistant),
content (Text), meta (JSON),  # meta stores {"strategy_proposal": {...}}
created_at
```

#### Setting
```python
id, key (unique), value (Text), updated_at
```

#### SystemLog
```python
id, level (DEBUG/INFO/WARNING/ERROR/CRITICAL),
module, message, details (JSON), created_at
```

### Helper Constants (in models.py)

```python
LIFECYCLE_STAGE_LABELS = {
    1: ("Generation",       "#888888"),
    2: ("Backtesting",      "#4488CC"),
    3: ("Walk-Forward",     "#AA66CC"),
    4: ("Out-of-Sample",    "#FF9800"),
    5: ("Shadow Trading",   "#00AA88"),
    6: ("Live Trading",     "#00CC77"),
}

STRATEGY_TYPE_META = {
    "rule":     ("RULE",     "#4488CC"),
    "ai":       ("AI",       "#AA44CC"),
    "ml":       ("ML",       "#CC8800"),
    "ensemble": ("ENSEMBLE", "#008888"),
}
```

### Helper Functions (in models.py)

```python
def promote_strategy_lifecycle(strategy_id: int) -> int:
    """Increment strategy.lifecycle_stage by 1, max 6. Returns new stage."""

def get_or_create_conversation(title: str = None) -> AIConversation:
    """Create a new AIConversation."""
```

---

## 6. Configuration & Settings

### AppSettings Singleton (`config/settings.py`)

YAML-backed configuration with dot-notation access. Stored at `config.yaml`.

```python
class AppSettings:
    def get(self, key_path: str, default=None)     # e.g. "risk.max_position_pct"
    def set(self, key_path: str, value)             # saves immediately
    def get_section(self, section: str) -> dict
    def load(self)
    def save(self)

settings = AppSettings()  # module-level singleton
```

### Default Configuration

```yaml
app:
  theme: dark
  language: en
  auto_start_feeds: false

risk:
  max_position_pct: 2.0
  max_portfolio_drawdown_pct: 15.0
  max_strategy_drawdown_pct: 10.0
  min_sharpe_live: 0.5
  max_spread_pct: 0.3
  max_open_positions: 10
  default_stop_loss_pct: 2.0
  default_take_profit_pct: 4.0

ai:
  openai_api_key: ""
  anthropic_api_key: ""
  openai_model: gpt-4o
  anthropic_model: claude-opus-4-6
  strategy_generation_enabled: true
  ml_confidence_threshold: 0.65
  retrain_interval_hours: 24

sentiment:
  news_enabled: true
  news_api_key: ""
  reddit_enabled: false
  reddit_client_id: ""
  reddit_client_secret: ""
  twitter_enabled: false
  onchain_enabled: false
  update_interval_minutes: 15

backtesting:
  default_fee_pct: 0.1
  default_slippage_pct: 0.05
  default_initial_capital: 10000.0
  walk_forward_train_months: 24
  walk_forward_validate_months: 6
  walk_forward_step_months: 3

data:
  default_timeframe: 1h
  historical_days: 365
  max_candles_per_request: 1000
  cache_enabled: true

notifications:
  desktop_enabled: true
  sound_enabled: false
  trade_alerts: true
  strategy_alerts: true
  system_alerts: true
```

### Constants (`config/constants.py`)

```python
APP_NAME        = "Nexus Trader"
APP_VERSION     = "1.0.0"
APP_DESCRIPTION = "Institutional-Grade AI Trading Platform"

DB_PATH     = "data/nexus_trader.db"
CONFIG_PATH = "config.yaml"
LOGS_DIR    = "logs/"
MODELS_DIR  = "data/models/"
CACHE_DIR   = "data/cache/"

SUPPORTED_EXCHANGES = ["kucoin", "binance", "bybit", "coinbase", "kraken", "okx"]

SUPPORTED_TIMEFRAMES = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d", "1w"]

LIFECYCLE_STAGES = [
    "Strategy Generation",
    "Backtesting",
    "Walk-Forward Optimization",
    "Out-of-Sample Testing",
    "Shadow Trading",
    "Live Trading",
]

STRATEGY_STATUSES = [
    "draft", "backtesting", "walk_forward", "out_of_sample",
    "shadow", "paper", "live", "disabled", "failed"
]

MARKET_REGIMES = [
    "bull_trend", "bear_trend", "ranging",
    "volatility_expansion", "volatility_compression"
]

ORDER_TYPES = ["market", "limit", "stop_limit", "stop_market"]
ORDER_SIDES = ["buy", "sell"]
TRADE_MODES = ["backtest", "shadow", "paper", "live"]
```

---

## 7. Core Engine — Backtesting

### File: `core/backtesting/backtest_engine.py`

### OPERATORS (14)

```python
OPERATORS = [
    ("greater than",              ">"),
    ("lower than",                "<"),
    ("greater than or equal to",  ">="),
    ("lower than or equal to",    "<="),
    ("equals",                    "=="),
    ("crosses above",             "crosses_above"),
    ("crosses below",             "crosses_below"),
    ("crosses above signal line", "crosses_above_signal"),
    ("crosses below signal line", "crosses_below_signal"),
    ("increased by % within",     "pct_up"),
    ("decreased by % within",     "pct_down"),
]
```

### INDICATOR_OPTIONS (60+)

Each entry is `(label: str, column_name: str)`.

```python
INDICATOR_OPTIONS = [
    # Price & Volume
    ("Price (Close)", "close"), ("Open", "open"), ("High", "high"),
    ("Low", "low"), ("Volume", "volume"),

    # RSI variants
    ("RSI (2)",  "rsi_2"),  ("RSI (3)",  "rsi_3"),  ("RSI (5)",  "rsi_5"),
    ("RSI (6)",  "rsi_6"),  ("RSI (7)",  "rsi_7"),  ("RSI (8)",  "rsi_8"),
    ("RSI (12)", "rsi_12"), ("RSI (14)", "rsi_14"), ("RSI (24)", "rsi_24"),

    # Stochastic RSI
    ("Stoch RSI K (14)", "stoch_rsi_k"), ("Stoch RSI D (14)", "stoch_rsi_d"),

    # EMA variants
    ("EMA (2)",  "ema_2"),  ("EMA (3)",  "ema_3"),  ("EMA (5)",  "ema_5"),
    ("EMA (9)",  "ema_9"),  ("EMA (10)", "ema_10"), ("EMA (12)", "ema_12"),
    ("EMA (20)", "ema_20"), ("EMA (26)", "ema_26"), ("EMA (27)", "ema_27"),
    ("EMA (32)", "ema_32"), ("EMA (50)", "ema_50"), ("EMA (63)", "ema_63"),
    ("EMA (200)","ema_200"),

    # SMA variants
    ("SMA (2)",  "sma_2"),  ("SMA (3)",  "sma_3"),  ("SMA (5)",  "sma_5"),
    ("SMA (9)",  "sma_9"),  ("SMA (10)", "sma_10"), ("SMA (12)", "sma_12"),
    ("SMA (20)", "sma_20"), ("SMA (26)", "sma_26"), ("SMA (27)", "sma_27"),
    ("SMA (32)", "sma_32"), ("SMA (50)", "sma_50"), ("SMA (63)", "sma_63"),
    ("SMA (200)","sma_200"),

    # Bollinger Bands
    ("Bollinger Upper (20)", "bb_upper"),
    ("Bollinger Middle (20)", "bb_mid"),
    ("Bollinger Lower (20)", "bb_lower"),

    # MACD
    ("MACD Line", "macd"), ("MACD Signal Line", "macd_signal"),
    ("MACD Histogram", "macd_hist"),

    # Money Flow Index
    ("Money Flow Index (14)", "mfi"),

    # ATR variants
    ("ATR (2)",  "atr_2"),  ("ATR (3)",  "atr_3"),  ("ATR (5)",  "atr_5"),
    ("ATR (6)",  "atr_6"),  ("ATR (7)",  "atr_7"),  ("ATR (8)",  "atr_8"),
    ("ATR (12)", "atr_12"), ("ATR (14)", "atr_14"), ("ATR (24)", "atr_24"),

    # SuperTrend
    ("SuperTrend (5)",  "supertrend_5"),
    ("SuperTrend (10)", "supertrend_10"),
    ("SuperTrend (15)", "supertrend_15"),

    # TWAP / VWAP
    ("TWAP", "twap"), ("VWAP", "vwap"),

    # Legacy / Misc
    ("ADX (14)",         "adx"),
    ("CCI (20)",         "cci"),
    ("Williams %R",      "williams_r"),
    ("Ichi Conversion",  "ichi_conversion"),
    ("Ichi Base Line",   "ichi_base"),
]
```

### RuleEvaluator

Evaluates a condition tree against a pandas DataFrame row-by-row.

**Condition Tree Node Formats:**

```python
# Leaf: compare indicator to numeric value
{
    "type":      "condition",
    "lhs":       "rsi_14",       # DataFrame column name
    "op":        ">",            # operator code
    "rhs_type":  "value",
    "rhs_value": 30.0
}

# Leaf: compare indicator to another indicator
{
    "type":          "condition",
    "lhs":           "ema_9",
    "op":            "crosses_above",
    "rhs_type":      "indicator",
    "rhs_indicator": "ema_21"
}

# Group: AND / OR of sub-conditions
{
    "type":       "group",
    "logic":      "AND",         # or "OR"
    "conditions": [<node>, ...]
}
```

**Operator Semantics:**
- `>`, `<`, `>=`, `<=`, `==` — standard comparisons, NaN-safe (return False on NaN)
- `crosses_above` — `lhs[i-1] <= rhs[i-1]` and `lhs[i] > rhs[i]`
- `crosses_below` — `lhs[i-1] >= rhs[i-1]` and `lhs[i] < rhs[i]`
- `crosses_above_signal` — MACD-specific: lhs crosses above `macd_signal` column
- `crosses_below_signal` — MACD-specific: lhs crosses below `macd_signal` column
- `pct_up` — price increased by N% within last M bars
- `pct_down` — price decreased by N% within last M bars

### run_backtest()

```python
def run_backtest(
    entry_tree: dict,
    exit_tree: Optional[dict],
    df_raw: pd.DataFrame,
    *,
    initial_capital: float = 10_000.0,
    position_size_pct: float = 10.0,
    stop_loss_pct: float = 2.0,
    take_profit_pct: float = 4.0,
    fee_pct: float = 0.10,
    slippage_pct: float = 0.05,
    direction: str = "long"        # "long" or "short"
) -> dict
```

**Returns:**
```python
{
    "trades":       list[dict],   # each trade: entry/exit price+time, pnl, exit_reason
    "equity_curve": list[float],  # equity value at each bar
    "metrics":      dict,         # win_rate, profit_factor, sharpe, max_drawdown, etc.
    "candle_count": int
}
```

**Simulation Logic:**
1. Run `calculate_all(df_raw)` to add indicator columns
2. Iterate bars from index 1 onward
3. If no open position: evaluate `entry_tree` → on signal, open position at `close * (1 + slippage)`
4. Per bar: check stop-loss, take-profit, then evaluate `exit_tree`
5. On exit: close at `close * (1 - slippage)`, deduct fee on both legs
6. Final metrics: total return %, annualized return, Sharpe ratio, Sortino ratio, max drawdown, win rate, profit factor

---

## 8. Core Engine — Indicator Library

### File: `core/features/indicator_library.py`

Uses the `ta` library. All indicators are computed in one pass via `calculate_all(df)`.

```python
def calculate_all(df: pd.DataFrame, config: dict = None) -> pd.DataFrame:
    """
    Compute all supported technical indicators on a standard OHLCV DataFrame.
    Input columns required: open, high, low, close, volume
    Returns same DataFrame with all indicator columns appended.
    """
```

**Indicators Computed:**

| Category | Indicators |
|---|---|
| Trend | EMA (2,3,5,9,10,12,20,26,27,32,50,63,200), SMA (same periods), WMA(20), VWAP, TWAP(14) |
| MACD | macd, macd_signal, macd_hist |
| Momentum | rsi_2/3/5/6/7/8/12/14/24, stoch_rsi_k/d(14), stoch_k/d, cci(20), roc, williams_r |
| Volatility | bb_upper/mid/lower/width/pct(20), kc_upper/mid/lower(20), dc_upper/mid/lower(20) |
| ATR | atr_2/3/5/6/7/8/12/14/24 |
| SuperTrend | supertrend_5/10/15 |
| Ichimoku | ichi_conversion(9), ichi_base(26), ichi_a, ichi_b |
| Volume | obv, accumulation_distribution, mfi(14), chaikin_mf(20) |

---

## 9. Core Engine — AI Module

### 9.1 LLM Provider (`core/ai/llm_provider.py`)

```python
@dataclass
class LLMMessage:
    role: str      # "user" or "assistant"
    content: str

class LLMProvider(ABC):
    @abstractmethod
    def stream_chat(
        self,
        messages: list[LLMMessage],
        system_prompt: str = "",
        max_tokens: int = 2048
    ) -> Iterator[str]: ...

    @property
    @abstractmethod
    def model_name(self) -> str: ...

    @property
    @abstractmethod
    def provider_name(self) -> str: ...

class ClaudeProvider(LLMProvider):
    """Uses anthropic.Anthropic(api_key=).messages.stream() for streaming."""
    default_model = "claude-opus-4-6"

class OpenAIProvider(LLMProvider):
    """Uses openai.OpenAI().chat.completions.create(stream=True)."""
    default_model = "gpt-4o"

def get_provider() -> Optional[LLMProvider]:
    """
    Factory. Priority: Anthropic → OpenAI.
    Reads api keys from settings (ai.anthropic_api_key, ai.openai_api_key).
    Returns None if neither is configured.
    """
```

### 9.2 Strategy Agent (`core/ai/strategy_agent.py`)

**System Prompt (verbatim content):**

The system prompt instructs the AI to:
- Act as an expert quantitative trading analyst
- Design complete, backtest-ready trading strategies
- Use only supported indicators and timeframes
- Explain reasoning in plain language before presenting any strategy
- Answer follow-up questions about performance and optimization
- Always wrap a proposed strategy in `<strategy_config>...</strategy_config>` XML tags

**Available Indicators** (communicated to AI):
RSI, MACD (Line, Signal, Histogram), Bollinger Bands (Upper/Mid/Lower), EMA, SMA, ATR, Stochastic RSI (%K/%D), ADX, OBV, VWAP, Ichimoku Cloud (Tenkan/Kijun/Senkou A/B), MFI, SuperTrend

**Available Timeframes:** `1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 6h, 12h, 1d, 1w`

**Market Regimes Tracked:** `bull_trend, bear_trend, ranging, volatility_expansion, volatility_compression`

**Strategy Proposal JSON Format** (inside `<strategy_config>` tags):
```json
{
  "name": "Descriptive Strategy Name",
  "type": "rule",
  "description": "2–3 sentence plain-language description of what the strategy does",
  "definition": {
    "symbols": ["BTC/USDT"],
    "timeframe": "1h",
    "indicators": [
      {"name": "RSI", "period": 14},
      {"name": "EMA", "period": 20, "label": "EMA20"}
    ],
    "entry_long":  {"conditions": ["RSI crosses above 30", "price above EMA20"], "logic": "AND"},
    "exit_long":   {"conditions": ["RSI crosses above 70", "price below EMA20"], "logic": "OR"},
    "entry_short": {"conditions": [], "logic": "AND"},
    "exit_short":  {"conditions": [], "logic": "OR"},
    "risk": {
      "stop_loss_pct": 2.0,
      "take_profit_pct": 4.0,
      "position_size_pct": 2.0,
      "max_concurrent_positions": 1
    }
  }
}
```

**Helper Functions:**
```python
def build_system_prompt(context: dict) -> str:
    """Inject dynamic context: active_strategy, recent_strategies, market_snapshot."""

def extract_strategy_proposal(text: str) -> Optional[dict]:
    """Regex-parse <strategy_config>...</strategy_config> block, return parsed JSON or None."""

def strip_strategy_config_blocks(text: str) -> str:
    """Remove proposal blocks from text for display in chat bubble."""
```

### 9.3 Condition Parser (`core/ai/condition_parser.py`)

Translates AI-generated natural language conditions into RuleEvaluator tree nodes.

**Supported Patterns:**
- `"RSI crosses above 30"` → `{lhs: "rsi_14", op: "crosses_above", rhs_type: "value", rhs_value: 30.0}`
- `"MACD crosses above signal line"` → `{lhs: "macd", op: "crosses_above_signal"}`
- `"price above EMA20"` → `{lhs: "close", op: ">", rhs_type: "indicator", rhs_indicator: "ema_20"}`
- `"RSI > 50"` → `{lhs: "rsi_14", op: ">", rhs_type: "value", rhs_value: 50.0}`
- `"EMA9 crosses above EMA21"` → crossover between two indicator columns
- All text comparison synonyms: above/below/greater than/less than/over/under/is/crosses

**Key Functions:**
```python
def _resolve_indicator(text: str) -> Optional[str]:
    """Map human label → DataFrame column name (e.g. "RSI 14" → "rsi_14")."""

def parse_condition_text(text: str) -> Optional[dict]:
    """Parse one condition string → condition leaf node."""

def build_condition_tree(conditions: list[str], logic: str = "AND") -> Optional[dict]:
    """Convert list of condition strings → RuleEvaluator group/leaf node."""

def ai_definition_to_backtest_params(definition: dict) -> dict:
    """
    Convert full AI strategy definition dict → run_backtest() kwargs.
    Returns: {entry_tree, exit_tree, direction, stop_loss_pct,
              take_profit_pct, position_size_pct, parse_errors}
    """
```

---

## 10. Navigation & Main Window

### File: `gui/main_window.py`

### NAV_ITEMS (in order)
```python
NAV_ITEMS = [
    ("dashboard",             "Dashboard",              "◈", None),
    ("market_scanner",        "Market Scanner",         "⊡", "TRADING"),
    ("chart_workspace",       "Chart Workspace",        "⋈", None),
    ("rule_builder",          "Rule Builder",           "⊞", "STRATEGIES"),
    ("strategies",            "Strategies",             "◉", None),
    ("ai_strategy_lab",       "AI Strategy Lab",        "◈", None),
    ("backtesting",           "Backtesting",            "⊟", "RESEARCH"),
    ("paper_trading",         "Paper Trading",          "◎", None),
    ("news_sentiment",        "News & Sentiment",       "⊠", "INTELLIGENCE"),
    ("risk_management",       "Risk Management",        "⊘", "MANAGEMENT"),
    ("orders_positions",      "Orders & Positions",     "⊕", None),
    ("performance_analytics", "Performance Analytics",  "◈", None),
    ("logs",                  "Logs",                   "≡", "SYSTEM"),
    ("settings",              "Settings",               "⊙", None),
    ("exchange_management",   "Exchange Management",    "⊞", None),
]
```

Format: `(page_key, label, icon, section_header_before_this_item_or_None)`

### MainWindow Structure
- `QMainWindow` with `QSplitter` or `QHBoxLayout`: Sidebar (220px fixed) + QStackedWidget (pages)
- `ThemeManager.apply_dark_theme()` called on startup
- Pages instantiated lazily on first navigation
- Active page tracked, nav item highlighted with orange left border

### PageHeader Component
```python
class PageHeader(QWidget):
    def __init__(self, title: str, subtitle: str = "", parent=None)
    def add_action(self, widget: QWidget)  # Adds to right-aligned action bar
```

### StatusBar
- Displays: current time | exchange status | feed status
- Updated via EventBus `Topics.EXCHANGE_STATUS_CHANGED`

---

## 11. Page Specifications

### 11.1 Dashboard Page

**File:** `gui/pages/dashboard/dashboard_page.py`

**Layout:** PageHeader → 4×2 metric card grid → Live Ticker Table → System Status Panel

**8 Metric Cards (fixed grid, 4 per row):**

| Card | Default Value | Notes |
|---|---|---|
| PORTFOLIO VALUE | `$10,000.00` | Shows mode label below (Paper / Live) |
| TODAY'S P&L | `+$0.00 (0.00%)` | Green `#00CC77` when positive |
| OPEN POSITIONS | `0` | Shows mode below |
| ACTIVE STRATEGIES | `0` | Shows `X live · Y shadow` breakdown |
| WIN RATE (30D) | `—` | Shows "No trades yet" note |
| SHARPE RATIO | `—` | Shows "30-day rolling" note |
| MAX DRAWDOWN | `—` | Shows "All time" note |
| SIGNALS TODAY | `0` | Shows `X confirmed · Y rejected` |

**Live Ticker Table:**
- Default watch symbols: `BTC/USDT, ETH/USDT, SOL/USDT, BNB/USDT`
- Columns: Symbol | Last Price | 24h Change | Volume | 24h High | 24h Low
- Feed status badge: `⬤ Live` (green) / `⬤ Feed inactive` (gray)
- Refreshes every 5 seconds via background worker

**System Status Panel (5 rows):**
1. Exchange Connection — "Disconnected" (red) / "Connected — {exchange name}" (green)
2. Market Data Feed — "Inactive" (gray) / "Active" (green)
3. ML Models — "Not Loaded" (gray) / "Loaded ({n} models)" (green)
4. Strategy Engine — "Standby" (gray) / "Running ({n} strategies)" (green)
5. Risk Manager — "Active" (green) always

**Welcome Banner (shown until exchange connected):**
- Title: "Phase 2 active: charting, indicators and market scanner are live"
- Body: Quick-start instructions — Exchange Management → Sync Assets → Chart Workspace → Market Scan

---

### 11.2 Market Scanner Page

**File:** `gui/pages/market_scanner/scanner_page.py`

**Purpose:** Scan the market for tradeable opportunities using CoinGecko data + technical indicators

**Filter Bar (horizontal, above table):**

| Control | Type | Options |
|---|---|---|
| Quote Currency | QComboBox | USDT, BTC, ETH, BNB |
| Timeframe | QComboBox | 1h, 4h, 1d, 15m, 5m |
| Signal Filter | QComboBox | All, Bullish, Bearish, Neutral |
| Min Market Cap | QComboBox | 14 tiers (see below) |
| Symbol Search | QLineEdit | Pattern match |
| Scan / Refresh button | QPushButton | primary (orange) |

**MIN_MCAP_OPTIONS:**
```python
[
    ("Any market cap",  0),
    ("> $500M",         500e6),
    ("> $1B",           1e9),
    ("> $2B",           2e9),
    ("> $3B",           3e9),
    ("> $4B",           4e9),
    ("> $5B",           5e9),
    ("> $10B",          10e9),
    ("> $15B",          15e9),
    ("> $20B",          20e9),
    ("> $25B",          25e9),
    ("> $50B",          50e9),
    ("> $75B",          75e9),
    ("> $100B",         100e9),
]
```

**Table Columns (12):**

| # | Column | Format | Color Rules |
|---|---|---|---|
| 1 | Symbol | "BTC/USDT" | white |
| 2 | Mkt Cap | `$1.23T` / `$456.7B` / `$12.3M` | gray |
| 3 | Price | adaptive decimal precision | white |
| 4 | 1H % | `+1.23%` | green if positive, red if negative |
| 5 | 24H % | `+5.67%` | green if positive, red if negative |
| 6 | 7D % | `+12.34%` | green if positive, red if negative |
| 7 | 24H Volume | formatted with M/B suffix | gray |
| 8 | RSI | `42.5` | green if < 35, red if > 65, white otherwise |
| 9 | Signal | `● BULLISH` / `● BEARISH` / `● NEUTRAL` | green/red/gray |
| 10 | Strength | `72%` | white |
| 11 | Bull | count | green |
| 12 | Bear | count | red |

**Data Sources:**
- `CoinGeckoWorker(QThread)` — fetches market data from CoinGecko API (paginated, up to 250 coins)
- `ScannerWorker(QThread)` — calculates technical indicators per symbol using `calculate_all()`

**Status Bar:** Shows "Scanning {n} symbols…" during scan, "Last updated: {time}" after completion

---

### 11.3 Chart Workspace Page

**File:** `gui/pages/chart_workspace/chart_page.py`

**Purpose:** Interactive OHLCV candlestick chart with overlaid technical indicators

**Controls Bar:**
- Symbol selector (QComboBox, populated from assets in DB)
- Timeframe selector: 1m, 5m, 15m, 1h, 4h, 1d
- Indicator checkboxes / multi-select: EMA20, EMA50, EMA200, BB, VWAP, Volume
- Date range selector or "Last N candles" spinbox
- Load / Refresh button

**Chart Features (pyqtgraph):**
- Candlestick chart with OHLC bars colored green (close > open) / red (close < open)
- Volume histogram below main chart (semi-transparent, matching candle color)
- Overlay indicators rendered as colored lines: EMA20 (blue), EMA50 (orange), EMA200 (yellow), BB upper/lower (purple dashed), VWAP (teal)
- Crosshair with OHLCV readout
- Zoom / pan via mouse wheel and drag
- Dark background matching app theme

**Data:** Loads from OHLCV table in DB; if data not present, triggers `historical_loader.py` fetch

---

### 11.4 Rule Builder Page

**File:** `gui/pages/rule_builder/rule_builder_page.py`

**Purpose:** Visual drag-and-drop style rule builder for creating condition-based trading strategies

**Layout:** PageHeader → Rule Blocks area (scrollable) → Add Block buttons → Execution Config → Save Rule button

**Condition Block Types:**

| Block | Label Color | Meaning |
|---|---|---|
| IF | `#1E90FF` (blue) | First condition |
| AND IF | `#00CC77` (green) | Additional required condition |
| OR | `#FF9800` (orange) | Alternative condition |

**Each Block Contains:**
```
[IF / AND IF / OR]  [Indicator ▾]  [at TF ▾]  [Operator ▾]  [Value or Indicator ▾]  [✕]
```

**Indicator dropdown:** All INDICATOR_OPTIONS (60+ items, see Section 7)

**Timeframe dropdown (per condition, optional override):**
`Strategy TF (blank), 1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 8h, 12h, 1d, 1w`

**Operator dropdown:** All OPERATORS (14 items, see Section 7)

**RHS (right-hand side):** Togglable between numeric QDoubleSpinBox and indicator QComboBox

**Execution Configuration Section:**
- Order type: `LIMIT (FOK)` | `MARKET`
- Side: `BUY` | `SELL`
- Start: `Start immediately` | `Schedule (cron)`

**Bottom Controls:**
- "+ Add AND Condition" button (green)
- "+ Add OR Condition" button (orange)
- "Save Rule" button (primary orange) → saves to TradingRule table
- "Test Conditions" button → runs quick evaluation against recent data

**Rule Name Field:** QLineEdit at top

---

### 11.5 Strategies Page

**File:** `gui/pages/strategies/strategies_page.py`

**Purpose:** View, manage, and monitor all strategies across all types and lifecycle stages

**Layout:** PageHeader (with "New Strategy" button) → Filter Bar → Strategy Table

**Filter Bar:**
- Type filter: All, Rule, AI, ML, Ensemble (QComboBox)
- Stage filter: All stages, 1–6 (QComboBox)
- Status filter: All, Active, Disabled (QComboBox)
- Search box (QLineEdit)

**Strategy Table Columns:**
| Column | Content |
|---|---|
| Name | Strategy name with type badge `[AI]` / `[RULE]` / `[ML]` / `[ENSEMBLE]` colored by STRATEGY_TYPE_META |
| Stage | `Stage N: Label` colored by LIFECYCLE_STAGE_LABELS |
| Type | type string |
| Win Rate | from StrategyMetrics, "—" if none |
| Sharpe | from StrategyMetrics, "—" if none |
| Max DD | from StrategyMetrics, "—" if none |
| Status | current status |
| Actions | Backtest / Promote / Disable buttons |

**Implementation Note:** `refresh()` queries ALL strategies (all types), not just rule-based. The type badge and stage label are colored using LIFECYCLE_STAGE_LABELS and STRATEGY_TYPE_META constants from models.py.

---

### 11.6 AI Strategy Lab Page

**File:** `gui/pages/ai_strategy_lab/ai_lab_page.py`

**Purpose:** Conversational AI interface for strategy ideation, explanation, and implementation

**Layout:** Two-panel: Left sidebar (240px) + Right chat panel

**Left Sidebar:**
- "New Conversation" button (top)
- Conversation history list (scrollable) — each item shows title + creation timestamp
- Clicking a conversation loads its message history

**Right Chat Panel:**
- API key warning banner (orange, shown when no API key configured): `"⚠ No AI provider configured. Add your API key in Settings → AI & ML."`
- `ChatHistoryWidget` (scrollable message area)
- Welcome message shown on new conversation (assistant bubble with intro text)
- `ChatInputBar` at bottom

**Chat Input Bar:**
- 🎤 Voice button (disabled, tooltip: "Voice input — coming in a future update")
- QTextEdit (auto-resizes up to 5 lines, Enter = send, Shift+Enter = newline)
- Send button (orange, "▶ Send")

**Message Rendering:**
- **User messages:** Right-aligned, `#162845` background, `#1E90FF` right border (4px), rounded 8px
- **Assistant messages:** Left-aligned, `#14141E` background, `#1E2D40` border (1px), rounded 8px
- **Streaming:** New text chunks appended with blinking `▋` cursor; cursor removed on completion
- **Markdown rendering:** Code blocks (monospace, `#0A0E1A` bg), inline code (same), bold, italic, headers, bullet lists all converted to HTML for display in QLabel

**Strategy Proposal Card (StrategyProposalCard):**
- Rendered separately below the assistant bubble when AI embeds a `<strategy_config>` block
- Background: `#0A1E14`, border: 2px `#00AA66`, radius: 8px
- Header: "📋 Strategy Proposal"
- Strategy name (bold, green `#00FF88`)
- Description text (gray `#8899AA`)
- Badges row: Pair | Timeframe | SL | TP | Size — each badge `#0F1623` bg, `#1E2D40` border
- Collapsible "Show Details" section (raw JSON in monospace)
- "✓ Apply Strategy" button (green, full-width)

**Apply Strategy Flow:**
1. User clicks "✓ Apply Strategy" on proposal card
2. Strategy saved to DB: `Strategy(name=..., type="ai", ai_generated=True, lifecycle_stage=1, definition={...})`
3. `QMessageBox.information` shown: `"'{name}' has been saved to your Strategy Library and is ready for backtesting."`
4. Strategy appears in Strategies page with `[AI]` badge and Stage 1: Generation

**Persistence:**
- `AIConversation` and `AIMessage` records written to DB
- On returning to a conversation, full message history loaded and rendered
- AI always receives full conversation history (system prompt + all prior messages) for context continuity
- Strategy proposals stored in `AIMessage.meta = {"strategy_proposal": {...}}`

**Workers:**
- `_ChatWorker(QThread)`: `chunk_ready = Signal(str)`, `finished = Signal(str)`, `error = Signal(str)`
- Calls `provider.stream_chat(messages, system_prompt)` in thread
- `chunk_ready` emitted per text chunk for streaming display

---

### 11.7 Backtesting Page

**File:** `gui/pages/backtesting/backtesting_page.py`

**Purpose:** Run historical backtests on strategies, view equity curves, promote through lifecycle

**Layout:** PageHeader → Config panel (left) + Results panel (right)

**Config Panel (left ~380px):**
- Strategy selector (QComboBox): loads ALL strategies, labeled as `[AI]  Name` / `[RULE]  Name` etc.
- Symbol selector (QComboBox)
- Timeframe selector
- Date range: Start Date + End Date (QDateEdit)
- Initial Capital (QDoubleSpinBox, default from settings)
- Position Size % (QDoubleSpinBox)
- Stop Loss % (QDoubleSpinBox)
- Take Profit % (QDoubleSpinBox)
- Fee % (QDoubleSpinBox, default 0.1%)
- Slippage % (QDoubleSpinBox, default 0.05%)
- "▶ Run Backtest" button (primary orange, full-width)

**Results Panel (right):**
- Equity curve chart (pyqtgraph line chart, green `#00FF88` on dark bg)
- Metrics grid: Total Return, Annualized Return, Sharpe Ratio, Sortino Ratio, Max Drawdown, Win Rate, Profit Factor, Total Trades, Avg Trade Duration
- Trade log table: Entry Time | Exit Time | Direction | Entry Price | Exit Price | Net P&L | Exit Reason
- Drawdown curve chart (red area chart)
- "Promote to Stage N: Label" button (shown after successful backtest, hidden if stage 6)

**Strategy Routing:**
- Rule-based (`type == "rule"`): use `entry_tree`/`exit_tree` directly from `Strategy.definition`
- AI/ML/Ensemble: call `ai_definition_to_backtest_params(definition)` → extract `entry_tree`, `exit_tree`, `direction`, risk params
- If `parse_errors` list non-empty: show warning banner in results with error details
- If `entry_tree` is None: show error, disable promote button

**Promote Button Logic:**
- After successful backtest: `_promote_btn.setVisible(True)`, label = `"Promote to Stage N: Label"` (where N = current_stage + 1)
- Click → calls `promote_strategy_lifecycle(strategy_id)` → updates DB, refreshes button label
- If new stage == 6: button label = "✓ Live Trading — Stage 6 Reached", then hide
- On error: `_promote_btn.setVisible(False)`

**BacktestWorker(QThread):**
```python
class BacktestWorker(QThread):
    finished = Signal(dict)
    error    = Signal(str)
    progress = Signal(int)   # 0–100

    def run(self):
        # Route by strategy type
        stype = self.strategy.type
        if stype in ("ai", "ml", "ensemble"):
            self._run_ai_strategy()
        else:
            self._run_rule_strategy()
```

---

### 11.8 Paper Trading Page

**File:** `gui/pages/paper_trading/paper_trading_page.py`

**Purpose:** Simulate live trading with real market data but no real money

**Key Elements:**
- Virtual portfolio balance display
- Active paper positions table
- Paper trade history table
- Start/Stop simulation controls
- Paper P&L chart

---

### 11.9 News & Sentiment Page

**File:** `gui/pages/news_sentiment/news_sentiment_page.py`

**Purpose:** Aggregate and display news headlines and sentiment scores for watched assets

**Key Elements:**
- News feed list (headline, source, timestamp, sentiment badge: Positive/Negative/Neutral)
- Sentiment score timeline chart per asset
- Fear & Greed index display
- Reddit/Twitter sentiment indicators (when enabled in settings)
- Asset sentiment heatmap

---

### 11.10 Risk Management Page

**File:** `gui/pages/risk_management/risk_page.py`

**Purpose:** Monitor and enforce risk limits across the portfolio and individual strategies

**Key Elements:**
- Current risk exposure summary (portfolio-level metrics)
- Strategy-level risk table: Strategy | Mode | Drawdown | Sharpe | Status
- Risk limit alerts (orange/red banners when limits approached/exceeded)
- Position-level risk breakdown
- Drawdown history chart
- Risk parameter display (from settings, link to Settings page to modify)

---

### 11.11 Orders & Positions Page

**File:** `gui/pages/orders_positions/orders_page.py`

**Purpose:** View and manage all open orders and positions

**Two-tab layout:**

**Positions tab:**
Columns: Symbol | Strategy | Mode | Direction | Entry Price | Quantity | Current Price | Unrealized P&L | Stop Loss | Take Profit | Opened At | Actions (Close button)

**Orders tab:**
Columns: Symbol | Side | Type | Status | Price | Quantity | Filled | Remaining | Fee | Created At | Actions (Cancel button)

Both tables support sorting by any column.

---

### 11.12 Performance Analytics Page

**File:** `gui/pages/performance_analytics/analytics_page.py`

**Purpose:** Deep-dive performance analysis across strategies and time periods

**Key Elements:**
- Period selector: 7D / 30D / 90D / 1Y / All Time
- Strategy filter (multi-select)
- Equity curve chart (overlaid per selected strategies)
- Monthly returns heatmap
- Performance metrics table: Total Return | Annualized | Sharpe | Sortino | Calmar | Max DD | Win Rate | Profit Factor | Total Trades
- Rolling Sharpe chart (30-day window)
- Trade distribution histogram (P&L per trade)
- Best/Worst trades list

---

### 11.13 Exchange Management Page

**File:** `gui/pages/exchange_management/exchange_page.py`

**Purpose:** Connect and manage exchange API credentials

**Key Elements:**
- List of configured exchanges with status badges (Connected / Disconnected / Error)
- "Add Exchange" form:
  - Exchange selector (KuCoin, Binance, Bybit, Coinbase, Kraken, OKX)
  - API Key field (password)
  - API Secret field (password)
  - Passphrase field (optional, password, for KuCoin)
  - Sandbox mode toggle
  - "Test Connection" button → validates credentials, shows success/error banner
  - "Save Exchange" button
- Per-exchange: Edit / Delete / "Sync Assets" button
- Asset sync status: last synced timestamp, asset count
- API credentials stored encrypted in DB

---

### 11.14 Logs Page

**File:** `gui/pages/logs/logs_page.py`

**Purpose:** View system and audit logs

**Key Elements:**
- Level filter: ALL / DEBUG / INFO / WARNING / ERROR / CRITICAL
- Module filter (QComboBox populated from distinct module values in DB)
- Search/filter text box
- Logs table: Timestamp | Level | Module | Message (colored by level: green=INFO, yellow=WARNING, red=ERROR/CRITICAL, gray=DEBUG)
- Auto-refresh toggle
- "Clear Logs" button (with confirmation dialog)
- Log entries written by all modules via `SystemLog` model

---

### 11.15 Settings Page

**File:** `gui/pages/settings/settings_page.py`

**Purpose:** Configure all application behavior, risk parameters, AI thresholds, and integrations

**Layout:** PageHeader (with "💾 Save All Settings" button) → QTabWidget (5 tabs)

**Important implementation note:** ALL fields must load their current saved values via `settings.get(key, default)` when the page is built. Never hardcode empty string as default for API key fields — they would overwrite saved keys on save.

**Tab 1: ⊘ Risk Management**
SettingsSection "Position & Portfolio Risk":
| Label | Setting Key | Type | Min | Max | Default | Suffix |
|---|---|---|---|---|---|---|
| Max Position Size | risk.max_position_pct | double | 0.1 | 50.0 | 2.0 | % |
| Max Portfolio Drawdown | risk.max_portfolio_drawdown_pct | double | 1.0 | 100.0 | 15.0 | % |
| Max Strategy Drawdown | risk.max_strategy_drawdown_pct | double | 1.0 | 100.0 | 10.0 | % |
| Min Sharpe (Live) | risk.min_sharpe_live | double | 0.0 | 5.0 | 0.5 | — |
| Max Spread Filter | risk.max_spread_pct | double | 0.01 | 5.0 | 0.3 | % |
| Default Stop Loss | risk.default_stop_loss_pct | double | 0.1 | 50.0 | 2.0 | % |
| Default Take Profit | risk.default_take_profit_pct | double | 0.1 | 100.0 | 4.0 | % |

**Tab 2: ◈ AI & ML**
SettingsSection "AI & Language Models":
| Label | Setting Key | Type | Notes |
|---|---|---|---|
| OpenAI API Key | ai.openai_api_key | text (password) | Placeholder: `sk-...` |
| Anthropic API Key | ai.anthropic_api_key | text (password) | Placeholder: `sk-ant-...` |
| OpenAI Model | ai.openai_model | combo | gpt-4o, gpt-4-turbo, gpt-4, gpt-3.5-turbo |
| Anthropic Model | ai.anthropic_model | combo | claude-opus-4-6, claude-sonnet-4-6, claude-haiku-4-5-20251001 |
| ML Confidence Threshold | ai.ml_confidence_threshold | double | 0.5–0.99, default 0.65 |
| Model Retrain Interval | ai.retrain_interval_hours | double | 1–168, default 24, suffix "hours" |
| Enable AI Strategy Generation | ai.strategy_generation_enabled | checkbox | default True |

**Tab 3: ◎ Data & Feeds**
SettingsSection "Data & Market Feeds":
| Label | Setting Key | Type | Notes |
|---|---|---|---|
| Default Timeframe | data.default_timeframe | combo | 1m, 5m, 15m, 1h, 4h, 1d |
| Historical Data (Days) | data.historical_days | double | 30–1825, default 365, suffix "days" |
| Enable Data Cache | data.cache_enabled | checkbox | default True |

SettingsSection "Sentiment Data Sources":
| Label | Setting Key | Type | Notes |
|---|---|---|---|
| Crypto News API | sentiment.news_enabled | checkbox | default True |
| News API Key | sentiment.news_api_key | text (password) | Placeholder: CryptoCompare or NewsAPI key |
| Reddit Sentiment | sentiment.reddit_enabled | checkbox | default False |
| Reddit Client ID | sentiment.reddit_client_id | text | |
| Reddit Secret | sentiment.reddit_client_secret | text (password) | |

**Tab 4: ⊟ Backtesting**
SettingsSection "Backtesting Defaults":
| Label | Setting Key | Type | Min | Max | Default | Suffix |
|---|---|---|---|---|---|---|
| Default Trading Fee | backtesting.default_fee_pct | double | 0.0 | 2.0 | 0.1 | % (3 decimals) |
| Default Slippage | backtesting.default_slippage_pct | double | 0.0 | 1.0 | 0.05 | % (3 decimals) |
| Default Capital | backtesting.default_initial_capital | double | 100 | 10,000,000 | 10000 | USDT |
| WF Train Window | backtesting.walk_forward_train_months | double | 6 | 60 | 24 | months |
| WF Validate Window | backtesting.walk_forward_validate_months | double | 1 | 24 | 6 | months |

**Tab 5: ⊕ Notifications**
SettingsSection "Notification Preferences":
| Label | Setting Key | Default |
|---|---|---|
| Desktop Notifications | notifications.desktop_enabled | True |
| Trade Execution Alerts | notifications.trade_alerts | True |
| Strategy Health Alerts | notifications.strategy_alerts | True |
| System Health Alerts | notifications.system_alerts | True |

**Save Logic:**
"Save All Settings" button iterates all 5 SettingsSection instances, calls `section.get_values()`, writes each key via `settings.set(key, value)`. Shows `QMessageBox.information` on success, `QMessageBox.critical` on error.

---

## 12. Shared Widgets

### SettingsSection (`gui/pages/settings/settings_page.py`)

```python
class SettingsSection(QGroupBox):
    def add_double(key, label, value, min_val, max_val, decimals, suffix) -> QDoubleSpinBox
    def add_text(key, label, value, password=False, placeholder="") -> QLineEdit
    def add_combo(key, label, options, current) -> QComboBox
    def add_check(key, label, checked) -> QCheckBox
    def get_values() -> dict[str, Any]
```

### ChatBubble (`gui/widgets/chat_widget.py`)

```python
class ChatBubble(QFrame):
    def __init__(self, role: str, text: str = "", parent=None)
    def append_chunk(self, chunk: str)   # streaming: append text + show cursor
    def finalize(self, full_text: str)   # set final text, remove cursor
```

User role: right-aligned, `#162845` bg, `#1E90FF` right border (4px), 8px radius
Assistant role: left-aligned, `#14141E` bg, `#1E2D40` border (1px), 8px radius
Text rendered via markdown-to-HTML helper.

### StrategyProposalCard (`gui/widgets/chat_widget.py`)

```python
class StrategyProposalCard(QFrame):
    apply_clicked = Signal(dict)   # emits the proposal dict

    def __init__(self, proposal: dict, parent=None)
    # Shows: name, description, badges (pair/TF/SL/TP/size), collapsible JSON, Apply button
```

### ChatHistoryWidget (`gui/widgets/chat_widget.py`)

```python
class ChatHistoryWidget(QScrollArea):
    def add_message(self, role: str, text: str) -> ChatBubble
    def add_streaming_bubble(self) -> ChatBubble        # returns bubble to stream into
    def add_proposal_card(self, proposal: dict) -> StrategyProposalCard
    def clear_messages(self)
    # Auto-scrolls to bottom on content addition
```

### ChatInputBar (`gui/widgets/chat_widget.py`)

```python
class ChatInputBar(QFrame):
    message_submitted = Signal(str)   # emitted on Enter key or Send button

    def __init__(self, parent=None)
    def set_enabled(self, enabled: bool)
    def clear(self)
```

### Chart Widget (`gui/widgets/chart_widget.py`)

```python
class CandlestickChartWidget(QWidget):
    def __init__(self, parent=None)
    def load_data(self, df: pd.DataFrame)      # df with OHLCV columns
    def set_indicators(self, indicators: list) # list of (col_name, color, style)
    def clear(self)
```

Uses pyqtgraph `PlotWidget`. Custom `CandlestickItem` renders green/red bars.

---

## 13. Event Bus

### File: `core/event_bus.py`

```python
class Topics(Enum):
    EXCHANGE_STATUS_CHANGED   = "exchange.status_changed"
    FEED_STATUS_CHANGED       = "feed.status_changed"
    NEW_SIGNAL                = "signals.new"
    TRADE_EXECUTED            = "trades.executed"
    POSITION_UPDATED          = "positions.updated"
    STRATEGY_STATUS_CHANGED   = "strategies.status_changed"
    RISK_LIMIT_BREACHED       = "risk.limit_breached"
    ML_PREDICTION_READY       = "ml.prediction_ready"
    DATA_SYNC_COMPLETE        = "data.sync_complete"
    SETTINGS_CHANGED          = "settings.changed"

class EventBus(QObject):
    def subscribe(self, topic: Topics, callback: Callable)
    def publish(self, topic: Topics, data: dict = None)
    def unsubscribe(self, topic: Topics, callback: Callable)

bus = EventBus()  # module-level singleton
```

---

## 14. Behavioral Requirements

### General
- All long-running operations (data fetch, backtest, AI chat, indicator calculation) run in `QThread` workers. The GUI never blocks.
- All worker threads emit `finished`, `error`, and optionally `progress` signals. UI connects to these to update state.
- DB session is created per worker/operation. Not shared across threads.
- Settings are read at startup and live-read by workers (not cached in workers).

### Strategy Lifecycle
- A strategy starts at `lifecycle_stage = 1` ("Generation") when first saved (from Rule Builder or AI Strategy Lab)
- Stage can only advance forward (1→2→3→…→6), never backward
- Each stage has a meaningful color badge displayed wherever the strategy appears
- Backtesting (stage 2) must be completed before promoting to stage 3 (walk-forward)
- No automated enforcement of prerequisites in v1.0 — the Promote button is shown after any successful backtest

### AI Strategy Lab
- On first visit to the page, a new conversation is automatically started
- If no API key is configured, show orange warning banner and disable input
- API key is checked live when the page is shown (`showEvent`)
- The full conversation history (all messages) is passed to the LLM on every request for context continuity
- `<strategy_config>` blocks are never shown in the chat bubble; they are rendered as `StrategyProposalCard` widgets instead
- Full original assistant text (including the config block) is stored in DB for LLM context

### Backtesting
- Both rule-based and AI strategies are listed in the strategy dropdown
- Route by `strategy.type`: if `"ai"`, `"ml"`, or `"ensemble"` → use `ai_definition_to_backtest_params()`; else use `entry_tree`/`exit_tree` directly from `definition`
- Show a user-friendly error if condition parsing fails (entry_tree is None)
- Parse warnings (some conditions unparseable but some succeeded) shown as orange banner, not blocking

### Settings
- API key fields always load their current saved value (never hardcode `""`)
- All settings save immediately to YAML on "Save All Settings" click

### Market Scanner
- Table updates in-place during scan (rows appended as results arrive)
- Color-code RSI values: green if < 35, red if > 65
- Market cap formatted with T/B/M suffixes for readability

### Chart Workspace
- Indicator overlays toggled via checkboxes; changing selection re-renders chart
- Chart retains zoom/pan state when switching indicator visibility

---

## 15. Build & Run Instructions

### Installation
```bash
cd NexusTrader
pip install -r requirements.txt
```

### First Run
```bash
python main.py
```

On first run:
1. DB is created at `data/nexus_trader.db`
2. Default settings written to `config.yaml`
3. Application opens to Dashboard

### Directory Setup
`main.py` should create required directories on startup:
```python
import os
for d in ["data", "data/cache", "data/models", "logs"]:
    os.makedirs(d, exist_ok=True)
```

### Entry Point (`main.py`)
```python
import sys
from PySide6.QtWidgets import QApplication
from gui.main_window import MainWindow
from gui.theme.theme_manager import ThemeManager
from core.database.engine import init_db

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Nexus Trader")
    init_db()
    ThemeManager.apply_dark_theme(app)
    window = MainWindow()
    window.setMinimumSize(1280, 800)
    window.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
```

---

*End of NexusTrader Specification — Version 1.0.0*

*This document captures the complete state of the application as built. Any developer or AI assistant starting from this specification should produce a functionally equivalent system.*
