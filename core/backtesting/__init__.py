# ============================================================
# NEXUS TRADER — Backtesting Module
#
# Exports for backtesting components:
#   • HistoricalDataLoader: Fetches real OHLCV data from exchanges
#   • IDSSBacktester: Bar-by-bar replay of IDSS pipeline
# ============================================================

from core.backtesting.data_loader import (
    HistoricalDataLoader,
    InsufficientDataError,
    DataSourceInfo,
    get_historical_data_loader,
)
from core.backtesting.idss_backtester import IDSSBacktester

__all__ = [
    "HistoricalDataLoader",
    "InsufficientDataError",
    "DataSourceInfo",
    "get_historical_data_loader",
    "IDSSBacktester",
]
