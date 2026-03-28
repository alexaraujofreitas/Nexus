# ============================================================
# NEXUS TRADER — SQLAlchemy ORM Models (Full Schema)
# ============================================================

import json
from datetime import datetime
from typing import Optional
from sqlalchemy import (
    Integer, String, Float, Boolean, DateTime, Text, JSON,
    ForeignKey, UniqueConstraint, Index, Enum
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from core.database.engine import Base


# ── Exchanges ────────────────────────────────────────────────
class Exchange(Base):
    __tablename__ = "exchanges"

    id:                     Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    name:                   Mapped[str]           = mapped_column(String(100), nullable=False)
    exchange_id:            Mapped[str]           = mapped_column(String(50), nullable=False)  # ccxt id
    api_key_encrypted:      Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    api_secret_encrypted:   Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    api_passphrase_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sandbox_mode:           Mapped[bool]          = mapped_column(Boolean, default=False)
    demo_mode:              Mapped[bool]          = mapped_column(Boolean, default=False)
    is_active:              Mapped[bool]          = mapped_column(Boolean, default=False)
    testnet_url:            Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at:             Mapped[datetime]      = mapped_column(DateTime, default=datetime.utcnow)
    updated_at:             Mapped[datetime]      = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    assets:    Mapped[list["Asset"]]    = relationship("Asset",    back_populates="exchange")
    trades:    Mapped[list["Trade"]]    = relationship("Trade",    back_populates="exchange")
    orders:    Mapped[list["Order"]]    = relationship("Order",    back_populates="exchange")
    positions: Mapped[list["Position"]] = relationship("Position", back_populates="exchange")

    @property
    def mode(self) -> str:
        """Return 'demo', 'sandbox', or 'live'."""
        if self.demo_mode:
            return "demo"
        if self.sandbox_mode:
            return "sandbox"
        return "live"

    def __repr__(self):
        return f"<Exchange {self.name} ({self.mode})>"


# ── Assets ───────────────────────────────────────────────────
class Asset(Base):
    __tablename__ = "assets"

    id:               Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    exchange_id:      Mapped[int]           = mapped_column(ForeignKey("exchanges.id"), nullable=False)
    symbol:           Mapped[str]           = mapped_column(String(20), nullable=False)  # BTC/USDT
    base_currency:    Mapped[str]           = mapped_column(String(10), nullable=False)  # BTC
    quote_currency:   Mapped[str]           = mapped_column(String(10), nullable=False)  # USDT
    min_amount:       Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    min_cost:         Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    price_precision:  Mapped[int]           = mapped_column(Integer, default=8)
    amount_precision: Mapped[int]           = mapped_column(Integer, default=8)
    is_active:        Mapped[bool]          = mapped_column(Boolean, default=True)
    last_updated:     Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("exchange_id", "symbol", name="uq_asset_exchange_symbol"),
    )

    exchange:  Mapped["Exchange"]  = relationship("Exchange",  back_populates="assets")
    ohlcv:     Mapped[list["OHLCV"]] = relationship("OHLCV",  back_populates="asset", cascade="all, delete-orphan")
    features:  Mapped[list["Feature"]] = relationship("Feature", back_populates="asset", cascade="all, delete-orphan")
    signals:   Mapped[list["Signal"]] = relationship("Signal", back_populates="asset")
    trades:    Mapped[list["Trade"]] = relationship("Trade", back_populates="asset")
    positions: Mapped[list["Position"]] = relationship("Position", back_populates="asset")

    def __repr__(self):
        return f"<Asset {self.symbol} @ {self.exchange_id}>"


# ── OHLCV Data ───────────────────────────────────────────────
class OHLCV(Base):
    __tablename__ = "ohlcv"

    id:        Mapped[int]   = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id:  Mapped[int]   = mapped_column(ForeignKey("assets.id"), nullable=False)
    timeframe: Mapped[str]   = mapped_column(String(5), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    open:      Mapped[float] = mapped_column(Float, nullable=False)
    high:      Mapped[float] = mapped_column(Float, nullable=False)
    low:       Mapped[float] = mapped_column(Float, nullable=False)
    close:     Mapped[float] = mapped_column(Float, nullable=False)
    volume:    Mapped[float] = mapped_column(Float, nullable=False)

    __table_args__ = (
        UniqueConstraint("asset_id", "timeframe", "timestamp", name="uq_ohlcv"),
        Index("idx_ohlcv_asset_tf_ts", "asset_id", "timeframe", "timestamp"),
    )

    asset: Mapped["Asset"] = relationship("Asset", back_populates="ohlcv")

    def __repr__(self):
        return f"<OHLCV {self.asset_id} {self.timeframe} {self.timestamp} C={self.close}>"


# ── Features (Computed Indicators) ───────────────────────────
class Feature(Base):
    __tablename__ = "features"

    id:           Mapped[int]   = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id:     Mapped[int]   = mapped_column(ForeignKey("assets.id"), nullable=False)
    timeframe:    Mapped[str]   = mapped_column(String(5), nullable=False)
    timestamp:    Mapped[datetime] = mapped_column(DateTime, nullable=False)
    feature_name: Mapped[str]   = mapped_column(String(100), nullable=False)
    value:        Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    __table_args__ = (
        UniqueConstraint("asset_id", "timeframe", "timestamp", "feature_name", name="uq_feature"),
        Index("idx_feature_asset_tf_ts", "asset_id", "timeframe", "timestamp"),
    )

    asset: Mapped["Asset"] = relationship("Asset", back_populates="features")


# ── Strategies ───────────────────────────────────────────────
class Strategy(Base):
    __tablename__ = "strategies"

    id:               Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    name:             Mapped[str]           = mapped_column(String(200), nullable=False)
    description:      Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    type:             Mapped[str]           = mapped_column(String(20), nullable=False)  # rule, ai, ml, ensemble
    status:           Mapped[str]           = mapped_column(String(30), default="draft")
    lifecycle_stage:  Mapped[int]           = mapped_column(Integer, default=1)
    definition:       Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    ai_generated:     Mapped[bool]          = mapped_column(Boolean, default=False)
    ai_model_used:    Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    created_by:       Mapped[str]           = mapped_column(String(50), default="user")
    created_at:       Mapped[datetime]      = mapped_column(DateTime, default=datetime.utcnow)
    updated_at:       Mapped[datetime]      = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    metrics:  Mapped[list["StrategyMetrics"]] = relationship("StrategyMetrics", back_populates="strategy", cascade="all, delete-orphan")
    signals:  Mapped[list["Signal"]]          = relationship("Signal",          back_populates="strategy")
    trades:   Mapped[list["Trade"]]           = relationship("Trade",           back_populates="strategy")
    positions: Mapped[list["Position"]]       = relationship("Position",        back_populates="strategy")

    def __repr__(self):
        return f"<Strategy {self.name!r} [{self.status}] stage={self.lifecycle_stage}>"


# ── Strategy Performance Metrics ─────────────────────────────
class StrategyMetrics(Base):
    __tablename__ = "strategy_metrics"

    id:             Mapped[int]              = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_id:    Mapped[int]              = mapped_column(ForeignKey("strategies.id"), nullable=False)
    run_type:       Mapped[str]              = mapped_column(String(20), nullable=False)  # backtest, shadow, paper, live
    period_start:   Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    period_end:     Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    total_trades:   Mapped[int]              = mapped_column(Integer, default=0)
    win_rate:       Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    profit_factor:  Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    sharpe_ratio:   Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    sortino_ratio:  Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    max_drawdown:   Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    total_pnl:      Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    total_pnl_pct:  Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    avg_trade_duration_hrs: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    calculated_at:  Mapped[datetime]         = mapped_column(DateTime, default=datetime.utcnow)

    strategy: Mapped["Strategy"] = relationship("Strategy", back_populates="metrics")


# ── Signals ──────────────────────────────────────────────────
class Signal(Base):
    __tablename__ = "signals"

    id:                   Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_id:          Mapped[int]           = mapped_column(ForeignKey("strategies.id"), nullable=False)
    asset_id:             Mapped[int]           = mapped_column(ForeignKey("assets.id"), nullable=False)
    signal_type:          Mapped[str]           = mapped_column(String(20), nullable=False)  # entry_long, etc.
    timestamp:            Mapped[datetime]      = mapped_column(DateTime, nullable=False)
    price:                Mapped[float]         = mapped_column(Float, nullable=False)
    confidence:           Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    regime:               Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    timeframe_alignment:  Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    indicator_values:     Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    ai_prediction:        Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    sentiment_score:      Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    microstructure_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status:               Mapped[str]           = mapped_column(String(20), default="pending")
    rejection_reason:     Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at:           Mapped[datetime]      = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_signal_strategy_ts", "strategy_id", "timestamp"),
    )

    strategy: Mapped["Strategy"] = relationship("Strategy", back_populates="signals")
    asset:    Mapped["Asset"]    = relationship("Asset",    back_populates="signals")
    trades:   Mapped[list["Trade"]] = relationship("Trade", back_populates="signal")


# ── Trades ───────────────────────────────────────────────────
class Trade(Base):
    __tablename__ = "trades"

    id:           Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_id:  Mapped[Optional[int]] = mapped_column(ForeignKey("strategies.id"), nullable=True)
    signal_id:    Mapped[Optional[int]] = mapped_column(ForeignKey("signals.id"), nullable=True)
    asset_id:     Mapped[int]           = mapped_column(ForeignKey("assets.id"), nullable=False)
    exchange_id:  Mapped[int]           = mapped_column(ForeignKey("exchanges.id"), nullable=False)
    trade_type:   Mapped[str]           = mapped_column(String(20), nullable=False)  # backtest|shadow|paper|live
    side:         Mapped[str]           = mapped_column(String(5), nullable=False)   # buy|sell
    entry_time:   Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    exit_time:    Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    entry_price:  Mapped[float]         = mapped_column(Float, nullable=False)
    exit_price:   Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    quantity:     Mapped[float]         = mapped_column(Float, nullable=False)
    pnl:          Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pnl_pct:      Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fees:         Mapped[float]         = mapped_column(Float, default=0.0)
    slippage:     Mapped[float]         = mapped_column(Float, default=0.0)
    exit_reason:  Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    explanation:  Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # Full audit trail
    created_at:   Mapped[datetime]      = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_trade_strategy", "strategy_id"),
        Index("idx_trade_type_ts", "trade_type", "entry_time"),
    )

    strategy: Mapped[Optional["Strategy"]] = relationship("Strategy", back_populates="trades")
    signal:   Mapped[Optional["Signal"]]   = relationship("Signal",   back_populates="trades")
    asset:    Mapped["Asset"]              = relationship("Asset",     back_populates="trades")
    exchange: Mapped["Exchange"]           = relationship("Exchange",  back_populates="trades")
    orders:   Mapped[list["Order"]]        = relationship("Order",     back_populates="trade", cascade="all, delete-orphan")


# ── Orders ───────────────────────────────────────────────────
class Order(Base):
    __tablename__ = "orders"

    id:               Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id:         Mapped[Optional[int]] = mapped_column(ForeignKey("trades.id"), nullable=True)
    exchange_id:      Mapped[int]           = mapped_column(ForeignKey("exchanges.id"), nullable=False)
    exchange_order_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    symbol:           Mapped[str]           = mapped_column(String(20), nullable=False)
    order_type:       Mapped[str]           = mapped_column(String(20), nullable=False)
    side:             Mapped[str]           = mapped_column(String(5), nullable=False)
    price:            Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    amount:           Mapped[float]         = mapped_column(Float, nullable=False)
    filled:           Mapped[float]         = mapped_column(Float, default=0.0)
    remaining:        Mapped[float]         = mapped_column(Float, default=0.0)
    status:           Mapped[str]           = mapped_column(String(20), default="open")
    timestamp:        Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at:       Mapped[datetime]      = mapped_column(DateTime, default=datetime.utcnow)

    trade:    Mapped[Optional["Trade"]] = relationship("Trade",    back_populates="orders")
    exchange: Mapped["Exchange"]        = relationship("Exchange", back_populates="orders")


# ── Positions ────────────────────────────────────────────────
class Position(Base):
    __tablename__ = "positions"

    id:             Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_id:    Mapped[Optional[int]] = mapped_column(ForeignKey("strategies.id"), nullable=True)
    exchange_id:    Mapped[int]           = mapped_column(ForeignKey("exchanges.id"), nullable=False)
    asset_id:       Mapped[int]           = mapped_column(ForeignKey("assets.id"), nullable=False)
    position_type:  Mapped[str]           = mapped_column(String(10), nullable=False)  # paper|live
    side:           Mapped[str]           = mapped_column(String(5), nullable=False)
    entry_price:    Mapped[float]         = mapped_column(Float, nullable=False)
    current_price:  Mapped[float]         = mapped_column(Float, default=0.0)
    quantity:       Mapped[float]         = mapped_column(Float, nullable=False)
    unrealized_pnl: Mapped[float]         = mapped_column(Float, default=0.0)
    stop_loss:      Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    take_profit:    Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    is_open:        Mapped[bool]          = mapped_column(Boolean, default=True)
    opened_at:      Mapped[datetime]      = mapped_column(DateTime, default=datetime.utcnow)
    updated_at:     Mapped[datetime]      = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    strategy: Mapped[Optional["Strategy"]] = relationship("Strategy", back_populates="positions")
    exchange: Mapped["Exchange"]           = relationship("Exchange",  back_populates="positions")
    asset:    Mapped["Asset"]              = relationship("Asset",     back_populates="positions")


# ── Sentiment Data ───────────────────────────────────────────
class SentimentData(Base):
    __tablename__ = "sentiment_data"

    id:              Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id:        Mapped[Optional[int]] = mapped_column(ForeignKey("assets.id"), nullable=True)
    source:          Mapped[str]           = mapped_column(String(20), nullable=False)
    timestamp:       Mapped[datetime]      = mapped_column(DateTime, nullable=False)
    sentiment_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)   # -1.0 to 1.0
    narrative_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    attention_index: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    raw_data:        Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        Index("idx_sentiment_asset_ts", "asset_id", "timestamp"),
    )


# ── ML Models Registry ───────────────────────────────────────
class MLModel(Base):
    __tablename__ = "ml_models"

    id:                 Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    name:               Mapped[str]           = mapped_column(String(200), nullable=False)
    model_type:         Mapped[str]           = mapped_column(String(50), nullable=False)
    asset_id:           Mapped[Optional[int]] = mapped_column(ForeignKey("assets.id"), nullable=True)
    timeframe:          Mapped[Optional[str]] = mapped_column(String(5), nullable=True)
    version:            Mapped[int]           = mapped_column(Integer, default=1)
    accuracy:           Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    feature_importance: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    model_path:         Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    trained_at:         Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    valid_until:        Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    is_active:          Mapped[bool]          = mapped_column(Boolean, default=False)

    predictions: Mapped[list["ModelPrediction"]] = relationship("ModelPrediction", back_populates="model", cascade="all, delete-orphan")


# ── Model Predictions ────────────────────────────────────────
class ModelPrediction(Base):
    __tablename__ = "model_predictions"

    id:                 Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    model_id:           Mapped[int]           = mapped_column(ForeignKey("ml_models.id"), nullable=False)
    asset_id:           Mapped[int]           = mapped_column(ForeignKey("assets.id"), nullable=False)
    timestamp:          Mapped[datetime]      = mapped_column(DateTime, nullable=False)
    bullish_probability: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    bearish_probability: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    expected_return:    Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    confidence:         Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    __table_args__ = (
        Index("idx_prediction_model_ts", "model_id", "timestamp"),
    )

    model: Mapped["MLModel"] = relationship("MLModel", back_populates="predictions")


# ── Market Regimes ───────────────────────────────────────────
class MarketRegime(Base):
    __tablename__ = "market_regimes"

    id:         Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id:   Mapped[int]           = mapped_column(ForeignKey("assets.id"), nullable=False)
    timeframe:  Mapped[str]           = mapped_column(String(5), nullable=False)
    timestamp:  Mapped[datetime]      = mapped_column(DateTime, nullable=False)
    regime:     Mapped[str]           = mapped_column(String(30), nullable=False)
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    features:   Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        Index("idx_regime_asset_tf_ts", "asset_id", "timeframe", "timestamp"),
    )


# ── Portfolio Snapshots ──────────────────────────────────────
class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"

    id:               Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_type:    Mapped[str]           = mapped_column(String(10), nullable=False)  # paper|live
    timestamp:        Mapped[datetime]      = mapped_column(DateTime, default=datetime.utcnow)
    total_value:      Mapped[float]         = mapped_column(Float, default=0.0)
    cash_balance:     Mapped[float]         = mapped_column(Float, default=0.0)
    positions_value:  Mapped[float]         = mapped_column(Float, default=0.0)
    unrealized_pnl:   Mapped[float]         = mapped_column(Float, default=0.0)
    daily_pnl:        Mapped[float]         = mapped_column(Float, default=0.0)
    drawdown:         Mapped[float]         = mapped_column(Float, default=0.0)
    holdings:         Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        Index("idx_portfolio_type_ts", "snapshot_type", "timestamp"),
    )




# ── Strategy lifecycle helpers ────────────────────────────────
LIFECYCLE_STAGE_LABELS: dict[int, tuple[str, str]] = {
    1: ("Generation",     "#888888"),
    2: ("Backtesting",    "#4488CC"),
    3: ("Walk-Forward",   "#AA66CC"),
    4: ("Out-of-Sample",  "#FF9800"),
    5: ("Shadow Trading", "#00AA88"),
    6: ("Live Trading",   "#00CC77"),
}

STRATEGY_TYPE_META: dict[str, tuple[str, str]] = {
    "rule":     ("RULE",     "#4488CC"),
    "ai":       ("AI",       "#AA44CC"),
    "ml":       ("ML",       "#CC8800"),
    "ensemble": ("ENSEMBLE", "#008888"),
}


def promote_strategy_lifecycle(strategy_id: int) -> int:
    """
    Advance *strategy_id* to the next lifecycle stage (max 6).
    Returns the new lifecycle_stage integer.
    """
    from core.database.engine import get_session  # local import avoids circular deps
    with get_session() as s:
        strat = s.get(Strategy, strategy_id)
        if strat is None:
            return 1
        if strat.lifecycle_stage < 6:
            strat.lifecycle_stage += 1
        return strat.lifecycle_stage




# ── Backtest Results ──────────────────────────────────────────
class BacktestResult(Base):
    """Stores the full output of one backtest run."""
    __tablename__ = "backtest_results"

    id:               Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_id:      Mapped[Optional[int]] = mapped_column(ForeignKey("strategies.id"), nullable=True)
    strategy_name:    Mapped[str]           = mapped_column(String(200), nullable=False)
    symbol:           Mapped[str]           = mapped_column(String(20), nullable=False)
    timeframe:        Mapped[str]           = mapped_column(String(5), nullable=False)
    initial_capital:  Mapped[float]         = mapped_column(Float, default=10000.0)
    final_capital:    Mapped[float]         = mapped_column(Float, default=0.0)
    total_return_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_drawdown_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sharpe_ratio:     Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    win_rate:         Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    total_trades:     Mapped[int]           = mapped_column(Integer, default=0)
    profit_factor:    Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    run_config:       Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)   # strategy config snapshot
    equity_curve:     Mapped[Optional[list]] = mapped_column(JSON, nullable=True)   # list of floats
    trade_log:        Mapped[Optional[list]] = mapped_column(JSON, nullable=True)   # list of trade dicts
    created_at:       Mapped[datetime]      = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_backtest_strategy_ts", "strategy_id", "created_at"),
    )

    def __repr__(self):
        return f"<BacktestResult {self.strategy_name!r} {self.symbol} ret={self.total_return_pct}%>"


# ── Paper Trades (IDSS Paper Executor) ───────────────────────
class PaperTrade(Base):
    """
    Persists every trade closed by PaperExecutor.
    Intentionally has NO foreign-key constraints — it stores IDSS
    paper results independently of the live-trading asset/exchange
    registry so the paper account survives restarts without requiring
    any exchange to be configured.
    """
    __tablename__ = "paper_trades"

    id:          Mapped[int]            = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol:      Mapped[str]            = mapped_column(String(30), nullable=False)
    side:        Mapped[str]            = mapped_column(String(5),  nullable=False)   # buy | sell
    regime:      Mapped[str]            = mapped_column(String(40), default="")
    timeframe:   Mapped[str]            = mapped_column(String(10), default="")
    entry_price: Mapped[float]          = mapped_column(Float, nullable=False)
    exit_price:  Mapped[float]          = mapped_column(Float, nullable=False)
    stop_loss:   Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    take_profit: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    size_usdt:       Mapped[float]          = mapped_column(Float, nullable=False)
    # Position-sizing transparency columns (Session 30).
    # entry_size_usdt: original USDT deployed when the trade was opened.
    # exit_size_usdt:  USDT actually closed in this record (< entry for partial closes).
    # Both default to NULL so existing rows (before this schema change) are still
    # readable; to_dict() falls back to size_usdt when they are NULL.
    entry_size_usdt: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=None)
    exit_size_usdt:  Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=None)
    pnl_usdt:    Mapped[float]          = mapped_column(Float, nullable=False)
    pnl_pct:     Mapped[float]          = mapped_column(Float, nullable=False)
    score:       Mapped[float]          = mapped_column(Float, default=0.0)
    exit_reason: Mapped[str]            = mapped_column(String(30), default="")
    models_fired: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    rationale:   Mapped[Optional[str]]  = mapped_column(Text, nullable=True)
    duration_s:  Mapped[int]            = mapped_column(Integer, default=0)
    opened_at:   Mapped[str]            = mapped_column(String(40), nullable=False)   # ISO string
    closed_at:   Mapped[str]            = mapped_column(String(40), nullable=False)   # ISO string
    created_at:  Mapped[datetime]       = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_paper_trades_symbol", "symbol"),
        Index("idx_paper_trades_closed_at", "closed_at"),
    )

    def to_dict(self) -> dict:
        """Convert back to the trade-dict format used by PaperExecutor."""
        # For rows written before Session 30 the two new columns are NULL —
        # fall back to size_usdt so the UI always has a usable value.
        _esz = self.entry_size_usdt if self.entry_size_usdt is not None else self.size_usdt
        _xsz = self.exit_size_usdt  if self.exit_size_usdt  is not None else self.size_usdt
        return {
            "symbol":           self.symbol,
            "side":             self.side,
            "regime":           self.regime,
            "timeframe":        self.timeframe,
            "entry_price":      self.entry_price,
            "exit_price":       self.exit_price,
            "stop_loss":        self.stop_loss,
            "take_profit":      self.take_profit,
            "size_usdt":        self.size_usdt,
            "entry_size_usdt":  _esz,
            "exit_size_usdt":   _xsz,
            "pnl_usdt":         self.pnl_usdt,
            "pnl_pct":          self.pnl_pct,
            "score":            self.score,
            "exit_reason":      self.exit_reason,
            "models_fired":     self.models_fired or [],
            "rationale":        self.rationale or "",
            "duration_s":       self.duration_s,
            "opened_at":        self.opened_at,
            "closed_at":        self.closed_at,
        }


# ── Live Trades ───────────────────────────────────────────────
class LiveTrade(Base):
    """
    Persists every order placed (and closed) by LiveExecutor.
    No FK constraints — standalone table like PaperTrade.

    Status lifecycle:  "open" → "closed"
    An open record is written on entry; updated to closed on exit.
    If the app crashes mid-trade, an open record remains and can be
    used for reconciliation.
    """
    __tablename__ = "live_trades"

    id:             Mapped[int]            = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol:         Mapped[str]            = mapped_column(String(30), nullable=False)
    side:           Mapped[str]            = mapped_column(String(5),  nullable=False)   # buy | sell
    status:         Mapped[str]            = mapped_column(String(10), default="open")   # open | closed
    regime:         Mapped[str]            = mapped_column(String(40), default="")
    timeframe:      Mapped[str]            = mapped_column(String(10), default="")
    entry_price:    Mapped[float]          = mapped_column(Float, nullable=False)
    exit_price:     Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    stop_loss:      Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    take_profit:    Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    size_usdt:      Mapped[float]          = mapped_column(Float, nullable=False)
    pnl_usdt:       Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pnl_pct:        Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    score:          Mapped[float]          = mapped_column(Float, default=0.0)
    exit_reason:    Mapped[str]            = mapped_column(String(30), default="")
    models_fired:   Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    rationale:      Mapped[Optional[str]]  = mapped_column(Text, nullable=True)
    entry_order_id: Mapped[str]            = mapped_column(String(80), default="")
    exit_order_id:  Mapped[str]            = mapped_column(String(80), default="")
    duration_s:     Mapped[int]            = mapped_column(Integer, default=0)
    opened_at:      Mapped[str]            = mapped_column(String(40), nullable=False)
    closed_at:      Mapped[str]            = mapped_column(String(40), default="")
    created_at:     Mapped[datetime]       = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_live_trades_symbol",    "symbol"),
        Index("idx_live_trades_status",    "status"),
        Index("idx_live_trades_closed_at", "closed_at"),
    )

    def to_dict(self) -> dict:
        return {
            "symbol":         self.symbol,
            "side":           self.side,
            "status":         self.status,
            "regime":         self.regime,
            "timeframe":      self.timeframe,
            "entry_price":    self.entry_price,
            "exit_price":     self.exit_price,
            "stop_loss":      self.stop_loss,
            "take_profit":    self.take_profit,
            "size_usdt":      self.size_usdt,
            "pnl_usdt":       self.pnl_usdt,
            "pnl_pct":        self.pnl_pct,
            "score":          self.score,
            "exit_reason":    self.exit_reason,
            "models_fired":   self.models_fired or [],
            "rationale":      self.rationale or "",
            "entry_order_id": self.entry_order_id,
            "exit_order_id":  self.exit_order_id,
            "duration_s":     self.duration_s,
            "opened_at":      self.opened_at,
            "closed_at":      self.closed_at,
        }


# ── Settings ─────────────────────────────────────────────────
class Setting(Base):
    __tablename__ = "settings"

    key:        Mapped[str]           = mapped_column(String(200), primary_key=True)
    value:      Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    category:   Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    updated_at: Mapped[datetime]      = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ── System Logs ──────────────────────────────────────────────
class SystemLog(Base):
    __tablename__ = "system_logs"

    id:        Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime]      = mapped_column(DateTime, default=datetime.utcnow)
    level:     Mapped[str]           = mapped_column(String(10), nullable=False)
    module:    Mapped[str]           = mapped_column(String(100), nullable=False)
    message:   Mapped[str]           = mapped_column(Text, nullable=False)
    details:   Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        Index("idx_log_level_ts", "level", "timestamp"),
    )




# ── Agent Signals  (Multi-Agent Intelligence Layer) ──────────
class AgentSignal(Base):
    """
    Persists every signal published by an intelligence agent.

    Enables:
    - Retrospective analysis of agent accuracy
    - Agent signal correlation with subsequent price moves
    - Audit trail for orchestrator decisions
    - UI dashboard historical charts per agent
    """
    __tablename__ = "agent_signals"

    id:              Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_name:      Mapped[str]           = mapped_column(String(50), nullable=False, index=True)
    timestamp:       Mapped[datetime]      = mapped_column(DateTime, nullable=False, index=True)
    signal:          Mapped[float]         = mapped_column(Float, nullable=False)       # [-1, +1]
    confidence:      Mapped[float]         = mapped_column(Float, nullable=False)       # [0, 1]
    is_stale:        Mapped[bool]          = mapped_column(Boolean, default=False)
    symbol:          Mapped[Optional[str]] = mapped_column(String(20), nullable=True)   # null = global signal
    topic:           Mapped[str]           = mapped_column(String(100), nullable=False) # EventBus topic
    payload:         Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)        # full agent output
    # Orchestrator context at time of signal
    regime_bias:     Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    macro_risk_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    macro_veto:      Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    __table_args__ = (
        Index("idx_agent_signals_agent_ts",  "agent_name", "timestamp"),
        Index("idx_agent_signals_symbol_ts", "symbol",     "timestamp"),
    )

    def __repr__(self):
        return (
            f"<AgentSignal {self.agent_name} "
            f"sig={self.signal:+.3f} conf={self.confidence:.2f} "
            f"@ {self.timestamp}>"
        )


class SignalLog(Base):
    """
    Flat log of every IDSS OrderCandidate that passed the Confluence Scorer.
    Used by the Signal Explorer page to browse historical signals.

    Written by the RiskGate / OrderRouter after confluence scoring.
    Fields are intentionally flat (no FK joins) for fast signal browsing.
    """
    __tablename__ = "signal_log"

    id:           Mapped[int]            = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp:    Mapped[datetime]       = mapped_column(DateTime, nullable=False, index=True)
    symbol:       Mapped[str]            = mapped_column(String(30), nullable=False, index=True)
    strategy_name:Mapped[str]            = mapped_column(String(80), nullable=False, index=True)
    direction:    Mapped[str]            = mapped_column(String(10), nullable=False)   # long | short
    strength:     Mapped[float]          = mapped_column(Float, nullable=False)        # confluence score 0-1
    entry_price:  Mapped[float]          = mapped_column(Float, nullable=False)
    stop_loss:    Mapped[float]          = mapped_column(Float, nullable=False)
    take_profit:  Mapped[float]          = mapped_column(Float, nullable=False)
    regime:       Mapped[Optional[str]]  = mapped_column(String(40), nullable=True)
    timeframe:    Mapped[Optional[str]]  = mapped_column(String(10), nullable=True)
    rationale:    Mapped[Optional[str]]  = mapped_column(Text,       nullable=True)
    models_fired: Mapped[Optional[dict]] = mapped_column(JSON,       nullable=True)   # list[str]
    approved:     Mapped[bool]           = mapped_column(Boolean, default=False)      # passed RiskGate
    rejection_reason: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    __table_args__ = (
        Index("idx_signal_log_symbol_ts", "symbol", "timestamp"),
        Index("idx_signal_log_strategy",  "strategy_name", "timestamp"),
    )

    def __repr__(self):
        return (
            f"<SignalLog {self.symbol} {self.direction} "
            f"score={self.strength:.3f} @ {self.timestamp}>"
        )


# ── Trade Feedback (AI Trade Analysis — Session 35) ───────────
class TradeFeedback(Base):
    """
    Persists the output of TradeAnalysisService for every closed paper trade.
    Enables adaptive learning aggregation by symbol, regime, model, and root cause.

    trade_id = symbol + "_" + opened_at (stable unique key).
    No FK constraints — standalone table like PaperTrade.
    """
    __tablename__ = "trade_feedback"

    id:               Mapped[int]            = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id:         Mapped[str]            = mapped_column(String(100), nullable=False, unique=True)
    symbol:           Mapped[str]            = mapped_column(String(30), nullable=False)
    side:             Mapped[str]            = mapped_column(String(5),  nullable=False)
    regime:           Mapped[str]            = mapped_column(String(40), default="")
    models_fired:     Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    # ── Scores (0-100) ────────────────────────────────────────
    setup_score:      Mapped[float]          = mapped_column(Float, default=0.0)
    risk_score:       Mapped[float]          = mapped_column(Float, default=0.0)
    execution_score:  Mapped[float]          = mapped_column(Float, default=0.0)
    decision_score:   Mapped[float]          = mapped_column(Float, default=0.0)
    overall_score:    Mapped[float]          = mapped_column(Float, default=0.0)

    # ── Classification ────────────────────────────────────────
    classification:   Mapped[str]            = mapped_column(String(10), default="NEUTRAL")  # GOOD | BAD | NEUTRAL
    hard_overrides:   Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    # ── Analysis JSON ─────────────────────────────────────────
    root_causes:      Mapped[Optional[list]] = mapped_column(JSON, nullable=True)   # list of root-cause dicts
    recommendations:  Mapped[Optional[list]] = mapped_column(JSON, nullable=True)   # list of recommendation dicts
    penalty_log:      Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)   # per-dimension penalty entries
    ai_explanation:   Mapped[Optional[str]]  = mapped_column(Text, nullable=True)   # Ollama explanation

    # ── Outcome (denormalised for fast aggregation queries) ───
    pnl_usdt:         Mapped[float]          = mapped_column(Float, default=0.0)
    pnl_pct:          Mapped[float]          = mapped_column(Float, default=0.0)
    exit_reason:      Mapped[str]            = mapped_column(String(30), default="")
    duration_s:       Mapped[int]            = mapped_column(Integer, default=0)

    created_at:       Mapped[datetime]       = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_feedback_symbol",         "symbol"),
        Index("idx_feedback_regime",         "regime"),
        Index("idx_feedback_classification", "classification"),
        Index("idx_feedback_created_at",     "created_at"),
    )

    # ── Phase 2: Decision forensics ──────────────────────────
    decision_outcome_matrix:     Mapped[Optional[str]]  = mapped_column(String(40),  nullable=True)
    avoidable_loss_flag:         Mapped[Optional[bool]] = mapped_column(Boolean,     nullable=True)
    avoidable_win_flag:          Mapped[Optional[bool]] = mapped_column(Boolean,     nullable=True)
    was_loss_acceptable:         Mapped[Optional[bool]] = mapped_column(Boolean,     nullable=True)
    failure_domain_primary:      Mapped[Optional[str]]  = mapped_column(String(30),  nullable=True)
    failure_domain_secondary:    Mapped[Optional[str]]  = mapped_column(String(30),  nullable=True)
    preventability_score:        Mapped[float]          = mapped_column(Float,       default=0.0)
    randomness_score:            Mapped[float]          = mapped_column(Float,       default=0.0)
    model_conflict_score:        Mapped[float]          = mapped_column(Float,       default=0.0)
    regime_confidence_at_entry:  Mapped[float]          = mapped_column(Float,       default=0.0)
    # ── Phase 2: Scoring extras ───────────────────────────────
    htf_confirmed_at_entry:      Mapped[Optional[bool]] = mapped_column(Boolean,     nullable=True)
    signal_conflict_score:       Mapped[float]          = mapped_column(Float,       default=0.0)

    def __repr__(self):
        return (
            f"<TradeFeedback {self.symbol} {self.classification} "
            f"overall={self.overall_score:.1f} @ {self.trade_id}>"
        )


# ── Strategy Tuning Proposal (Phase 2 — Adaptive Learning) ───
class StrategyTuningProposal(Base):
    """
    Represents a proposed parameter adjustment generated by the adaptive
    learning engine from recurring TradeFeedback root-cause patterns.

    Must be backtest-gated before promotion.
    """
    __tablename__ = "strategy_tuning_proposals"

    id:                      Mapped[int]            = mapped_column(Integer, primary_key=True, autoincrement=True)
    proposal_id:             Mapped[str]            = mapped_column(String(30),  nullable=False, unique=True)
    root_cause_category:     Mapped[str]            = mapped_column(String(50),  nullable=False)
    rec_id:                  Mapped[str]            = mapped_column(String(80),  nullable=False)
    trigger_evidence:        Mapped[Optional[dict]] = mapped_column(JSON,        nullable=True)
    affected_subsystem:      Mapped[str]            = mapped_column(String(100), default="")
    tuning_parameter:        Mapped[str]            = mapped_column(String(100), default="")
    tuning_direction:        Mapped[str]            = mapped_column(String(20),  default="manual")
    proposed_change_description: Mapped[str]        = mapped_column(Text,        default="")
    expected_benefit:        Mapped[str]            = mapped_column(Text,        default="")
    confidence:              Mapped[float]          = mapped_column(Float,       default=0.0)
    risk_level:              Mapped[str]            = mapped_column(String(10),  default="medium")
    auto_tune_eligible:      Mapped[bool]           = mapped_column(Boolean,     default=False)
    requires_manual_approval:Mapped[bool]           = mapped_column(Boolean,     default=True)
    status:                  Mapped[str]            = mapped_column(String(20),  default="pending")
    backtest_result:         Mapped[Optional[dict]] = mapped_column(JSON,        nullable=True)
    promoted_at:             Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    rejected_at:             Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    rejection_reason:        Mapped[Optional[str]]  = mapped_column(Text,        nullable=True)
    created_at:              Mapped[datetime]        = mapped_column(DateTime,    default=datetime.utcnow)

    __table_args__ = (
        Index("idx_proposal_status",    "status"),
        Index("idx_proposal_root_cause","root_cause_category"),
        Index("idx_proposal_param",     "tuning_parameter"),
    )

    def __repr__(self):
        return (
            f"<StrategyTuningProposal {self.proposal_id} "
            f"{self.tuning_parameter} [{self.status}]>"
        )


# ── Applied Strategy Change (Phase 2 — Audit Log) ────────────
class AppliedStrategyChange(Base):
    """
    Immutable audit record of every parameter change that was actually applied
    to NexusTrader's configuration. Each entry is versioned and traceable
    back to the original TuningProposal and backtest result.
    """
    __tablename__ = "applied_strategy_changes"

    id:                     Mapped[int]   = mapped_column(Integer,    primary_key=True, autoincrement=True)
    proposal_id:            Mapped[str]   = mapped_column(String(30), nullable=False)
    root_cause_category:    Mapped[str]   = mapped_column(String(50), default="")
    tuning_parameter:       Mapped[str]   = mapped_column(String(100), default="")
    tuning_direction:       Mapped[str]   = mapped_column(String(20), default="manual")
    applied_value:          Mapped[str]   = mapped_column(Text,       default="")
    applied_by:             Mapped[str]   = mapped_column(String(20), default="auto")
    notes:                  Mapped[str]   = mapped_column(Text,       default="")
    backtest_delta_pf_pct:  Mapped[float] = mapped_column(Float,      default=0.0)
    applied_at:             Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_applied_param",      "tuning_parameter"),
        Index("idx_applied_applied_at", "applied_at"),
    )

    def __repr__(self):
        return (
            f"<AppliedStrategyChange {self.proposal_id} "
            f"{self.tuning_parameter}={self.applied_value} @ {self.applied_at}>"
        )


# ── Tuning Proposal Outcome (Wave 2 — ROI Tracker) ───────────
class TuningProposalOutcome(Base):
    """
    Tracks the realized performance impact of applying a TuningProposal.

    One row per applied proposal.  Pre-application metrics are recorded at
    application time; post-application metrics accumulate as trades close.
    When post_trades reaches min_trades_threshold the verdict is computed and
    status advances to MEASURED.

    Lifecycle:  PENDING_EVALUATION → EVALUATING → MEASURED

    Design constraints:
      - Read-only after creation except for post-* and verdict columns
      - proposal_id maps 1:1 to StrategyTuningProposal.proposal_id (not FK to
        avoid cascade complications on in-memory SQLite test DBs)
      - All delta/verdict columns nullable — only populated when MEASURED
    """
    __tablename__ = "tuning_proposal_outcomes"

    id:                   Mapped[int]            = mapped_column(Integer, primary_key=True, autoincrement=True)
    proposal_id:          Mapped[str]            = mapped_column(String(30),  nullable=False, unique=True)

    # Lifecycle tracking
    status:               Mapped[str]            = mapped_column(String(30),  default="PENDING_EVALUATION")
    min_trades_threshold: Mapped[int]            = mapped_column(Integer,     default=30)

    # Pre-application baseline (recorded at application time)
    pre_trades:           Mapped[int]            = mapped_column(Integer,     default=0)
    pre_win_rate:         Mapped[Optional[float]]= mapped_column(Float,       nullable=True)
    pre_profit_factor:    Mapped[Optional[float]]= mapped_column(Float,       nullable=True)
    pre_avg_r:            Mapped[Optional[float]]= mapped_column(Float,       nullable=True)

    # Post-application results (updated as trades accumulate)
    post_trades:          Mapped[int]            = mapped_column(Integer,     default=0)
    post_win_rate:        Mapped[Optional[float]]= mapped_column(Float,       nullable=True)
    post_profit_factor:   Mapped[Optional[float]]= mapped_column(Float,       nullable=True)
    post_avg_r:           Mapped[Optional[float]]= mapped_column(Float,       nullable=True)

    # Computed deltas and verdict (populated when status transitions to MEASURED)
    delta_win_rate:       Mapped[Optional[float]]= mapped_column(Float,       nullable=True)
    delta_pf:             Mapped[Optional[float]]= mapped_column(Float,       nullable=True)
    verdict:              Mapped[Optional[str]]  = mapped_column(String(20),  nullable=True)  # IMPROVED | NEUTRAL | DEGRADED
    notes:                Mapped[Optional[str]]  = mapped_column(Text,        nullable=True)

    applied_at:           Mapped[datetime]       = mapped_column(DateTime,    default=datetime.utcnow)
    measured_at:          Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at:           Mapped[datetime]       = mapped_column(DateTime,    default=datetime.utcnow)

    __table_args__ = (
        Index("idx_tpo_status",      "status"),
        Index("idx_tpo_proposal_id", "proposal_id"),
    )

    def __repr__(self):
        return (
            f"<TuningProposalOutcome {self.proposal_id} "
            f"[{self.status}] post={self.post_trades}/{self.min_trades_threshold} "
            f"verdict={self.verdict}>"
        )
