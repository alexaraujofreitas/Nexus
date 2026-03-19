# ============================================================
# NEXUS TRADER — System Constants
# ============================================================

from pathlib import Path

# --- Application ---
APP_NAME = "Nexus Trader"
APP_VERSION = "1.0.0"
APP_DESCRIPTION = "Institutional-Grade AI Trading Platform"

# --- Paths ---
ROOT_DIR = Path(__file__).parent.parent
DATA_DIR = ROOT_DIR / "data"
LOGS_DIR = ROOT_DIR / "logs"
MODELS_DIR = DATA_DIR / "models"
CACHE_DIR = DATA_DIR / "cache"
DB_PATH = DATA_DIR / "nexus_trader.db"
CONFIG_PATH = ROOT_DIR / "config.yaml"

# Ensure directories exist
DATA_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)
MODELS_DIR.mkdir(exist_ok=True)
CACHE_DIR.mkdir(exist_ok=True)

# --- Supported Exchanges ---
SUPPORTED_EXCHANGES = {
    "kucoin":   "KuCoin",
    "binance":  "Binance",
    "bybit":    "Bybit",
    "coinbase": "Coinbase",
    "kraken":   "Kraken",
    "okx":      "OKX",
}

# --- Supported Timeframes ---
TIMEFRAMES = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d", "1w"]
TIMEFRAME_LABELS = {
    "1m": "1 Minute", "3m": "3 Minutes", "5m": "5 Minutes",
    "15m": "15 Minutes", "30m": "30 Minutes", "1h": "1 Hour",
    "2h": "2 Hours", "4h": "4 Hours", "6h": "6 Hours",
    "12h": "12 Hours", "1d": "1 Day", "1w": "1 Week",
}

# --- Strategy Lifecycle Stages ---
STRATEGY_STAGES = {
    1: "Strategy Generation",
    2: "Backtesting",
    3: "Walk-Forward Optimization",
    4: "Out-of-Sample Testing",
    5: "Shadow Trading",
    6: "Live Trading",
}

STRATEGY_STATUSES = ["draft", "backtesting", "walk_forward", "out_of_sample",
                     "shadow", "paper", "live", "disabled", "failed"]

# --- Market Regimes ---
MARKET_REGIMES = [
    "bull_trend", "bear_trend", "ranging",
    "volatility_expansion", "volatility_compression",
]

# --- Order Types ---
ORDER_TYPES = ["market", "limit", "stop_limit", "stop_market"]
ORDER_SIDES = ["buy", "sell"]

# --- Trade Modes ---
TRADE_MODES = ["backtest", "shadow", "paper", "live"]

# --- Risk Defaults (configurable in Settings) ---
DEFAULT_MAX_POSITION_PCT = 2.0        # Max 2% of portfolio per trade
DEFAULT_MAX_PORTFOLIO_DRAWDOWN = 15.0  # Auto-pause at 15% drawdown
DEFAULT_MAX_STRATEGY_DRAWDOWN = 10.0   # Auto-disable strategy at 10% drawdown
DEFAULT_MIN_SHARPE_LIVE = 0.5          # Min rolling 30-day Sharpe to stay live
DEFAULT_MAX_SPREAD_PCT = 0.3           # Reject if spread > 0.3%

# --- ML Model Types ---
ML_MODEL_TYPES = ["random_forest", "xgboost", "lightgbm", "logistic_regression"]

# --- Sentiment Sources ---
SENTIMENT_SOURCES = ["news", "reddit", "twitter", "onchain", "macro"]

# --- Logging ---
LOG_FILE = LOGS_DIR / "nexus_trader.log"
LOG_MAX_BYTES = 10 * 1024 * 1024  # 10MB
LOG_BACKUP_COUNT = 5

# --- UI ---
SIDEBAR_WIDTH = 220
SIDEBAR_COLLAPSED_WIDTH = 60
STATUSBAR_HEIGHT = 28

# --- Colors (Bloomberg Dark Theme) ---
COLOR_BG_PRIMARY = "#0A0E1A"
COLOR_BG_SECONDARY = "#0F1623"
COLOR_BG_PANEL = "#131B2E"
COLOR_BG_CARD = "#1A2332"
COLOR_ACCENT_ORANGE = "#FF6B00"
COLOR_ACCENT_BLUE = "#1E90FF"
COLOR_ACCENT_GREEN = "#00FF88"
COLOR_ACCENT_RED = "#FF3355"
COLOR_TEXT_PRIMARY = "#E8EBF0"
COLOR_TEXT_SECONDARY = "#8899AA"
COLOR_TEXT_MUTED = "#4A5568"
COLOR_BORDER = "#1E2D40"
COLOR_BORDER_ACTIVE = "#2D4A6B"
