# ============================================================
# NEXUS TRADER Web — Trading ORM Models (PostgreSQL)
#
# 1:1 mirror of core/database/models.py schema.
# Key differences from desktop:
#   - Uses app.database.Base (PostgreSQL) not core.database.engine.Base
#   - JSON columns use native PostgreSQL JSONB for indexing
#   - No _migrate_schema() — Alembic handles all migrations
#   - Relationship definitions preserved for ORM query convenience
# ============================================================
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean, DateTime, Float, Integer, String, Text,
    ForeignKey, UniqueConstraint, Index,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


# ── Exchanges ────────────────────────────────────────────────
class Exchange(Base):
    __tablename__ = "exchanges"

    id:                       Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    name:                     Mapped[str]           = mapped_column(String(100), nullable=False)
    exchange_id:              Mapped[str]           = mapped_column(String(50), nullable=False)
    api_key_encrypted:        Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    api_secret_encrypted:     Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    api_passphrase_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sandbox_mode:             Mapped[bool]          = mapped_column(Boolean, default=False)
    demo_mode:                Mapped[bool]          = mapped_column(Boolean, default=False)
    is_active:                Mapped[bool]          = mapped_column(Boolean, default=False)
    testnet_url:              Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at:               Mapped[datetime]      = mapped_column(DateTime, default=datetime.utcnow)
    updated_at:               Mapped[datetime]      = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    assets:    Mapped[list["Asset"]]    = relationship("Asset",    back_populates="exchange")
    trades:    Mapped[list["Trade"]]    = relationship("Trade",    back_populates="exchange")
    orders:    Mapped[list["Order"]]    = relationship("Order",    back_populates="exchange")
    positions: Mapped[list["Position"]] = relationship("Position", back_populates="exchange")

    @property
    def mode(self) -> str:
        if self.demo_mode:
            return "demo"
        if self.sandbox_mode:
            return "sandbox"
        return "live"


# ── Assets ───────────────────────────────────────────────────
class Asset(Base):
    __tablename__ = "assets"

    id:               Mapped[int]             = mapped_column(Integer, primary_key=True, autoincrement=True)
    exchange_id:      Mapped[int]             = mapped_column(ForeignKey("exchanges.id"), nullable=False)
    symbol:           Mapped[str]             = mapped_column(String(20), nullable=False)
    base_currency:    Mapped[str]             = mapped_column(String(10), nullable=False)
    quote_currency:   Mapped[str]             = mapped_column(String(10), nullable=False)
    min_amount:       Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    min_cost:         Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    price_precision:  Mapped[int]             = mapped_column(Integer, default=8)
    amount_precision: Mapped[int]             = mapped_column(Integer, default=8)
    is_active:        Mapped[bool]            = mapped_column(Boolean, default=True)
    last_updated:     Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("exchange_id", "symbol", name="uq_asset_exchange_symbol"),
    )

    exchange:  Mapped["Exchange"]       = relationship("Exchange",  back_populates="assets")
    ohlcv:     Mapped[list["OHLCV"]]    = relationship("OHLCV",     back_populates="asset", cascade="all, delete-orphan")
    features:  Mapped[list["Feature"]]  = relationship("Feature",   back_populates="asset", cascade="all, delete-orphan")
    signals:   Mapped[list["Signal"]]   = relationship("Signal",    back_populates="asset")
    trades:    Mapped[list["Trade"]]    = relationship("Trade",     back_populates="asset")
    positions: Mapped[list["Position"]] = relationship("Position",  back_populates="asset")


# ── OHLCV Data ───────────────────────────────────────────────
class OHLCV(Base):
    __tablename__ = "ohlcv"

    id:        Mapped[int]      = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id:  Mapped[int]      = mapped_column(ForeignKey("assets.id"), nullable=False)
    timeframe: Mapped[str]      = mapped_column(String(5), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    open:      Mapped[float]    = mapped_column(Float, nullable=False)
    high:      Mapped[float]    = mapped_column(Float, nullable=False)
    low:       Mapped[float]    = mapped_column(Float, nullable=False)
    close:     Mapped[float]    = mapped_column(Float, nullable=False)
    volume:    Mapped[float]    = mapped_column(Float, nullable=False)

    __table_args__ = (
        UniqueConstraint("asset_id", "timeframe", "timestamp", name="uq_ohlcv"),
        Index("idx_ohlcv_asset_tf_ts", "asset_id", "timeframe", "timestamp"),
    )

    asset: Mapped["Asset"] = relationship("Asset", back_populates="ohlcv")


# ── Features (Computed Indicators) ───────────────────────────
class Feature(Base):
    __tablename__ = "features"

    id:           Mapped[int]             = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id:     Mapped[int]             = mapped_column(ForeignKey("assets.id"), nullable=False)
    timeframe:    Mapped[str]             = mapped_column(String(5), nullable=False)
    timestamp:    Mapped[datetime]        = mapped_column(DateTime, nullable=False)
    feature_name: Mapped[str]             = mapped_column(String(100), nullable=False)
    value:        Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    __table_args__ = (
        UniqueConstraint("asset_id", "timeframe", "timestamp", "feature_name", name="uq_feature"),
        Index("idx_feature_asset_tf_ts", "asset_id", "timeframe", "timestamp"),
    )

    asset: Mapped["Asset"] = relationship("Asset", back_populates="features")


# ── Strategies ───────────────────────────────────────────────
class Strategy(Base):
    __tablename__ = "strategies"

    id:              Mapped[int]            = mapped_column(Integer, primary_key=True, autoincrement=True)
    name:            Mapped[str]            = mapped_column(String(200), nullable=False)
    description:     Mapped[Optional[str]]  = mapped_column(Text, nullable=True)
    type:            Mapped[str]            = mapped_column(String(20), nullable=False)
    status:          Mapped[str]            = mapped_column(String(30), default="draft")
    lifecycle_stage: Mapped[int]            = mapped_column(Integer, default=1)
    definition:      Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    ai_generated:    Mapped[bool]           = mapped_column(Boolean, default=False)
    ai_model_used:   Mapped[Optional[str]]  = mapped_column(String(100), nullable=True)
    created_by:      Mapped[str]            = mapped_column(String(50), default="user")
    created_at:      Mapped[datetime]       = mapped_column(DateTime, default=datetime.utcnow)
    updated_at:      Mapped[datetime]       = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    metrics:   Mapped[list["StrategyMetrics"]] = relationship("StrategyMetrics", back_populates="strategy", cascade="all, delete-orphan")
    signals:   Mapped[list["Signal"]]          = relationship("Signal",          back_populates="strategy")
    trades:    Mapped[list["Trade"]]           = relationship("Trade",           back_populates="strategy")
    positions: Mapped[list["Position"]]        = relationship("Position",        back_populates="strategy")


# ── Strategy Performance Metrics ─────────────────────────────
class StrategyMetrics(Base):
    __tablename__ = "strategy_metrics"

    id:                     Mapped[int]              = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_id:            Mapped[int]              = mapped_column(ForeignKey("strategies.id"), nullable=False)
    run_type:               Mapped[str]              = mapped_column(String(20), nullable=False)
    period_start:           Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    period_end:             Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    total_trades:           Mapped[int]              = mapped_column(Integer, default=0)
    win_rate:               Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    profit_factor:          Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    sharpe_ratio:           Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    sortino_ratio:          Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    max_drawdown:           Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    total_pnl:              Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    total_pnl_pct:          Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    avg_trade_duration_hrs: Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    calculated_at:          Mapped[datetime]          = mapped_column(DateTime, default=datetime.utcnow)

    strategy: Mapped["Strategy"] = relationship("Strategy", back_populates="metrics")


# ── Signals ──────────────────────────────────────────────────
class Signal(Base):
    __tablename__ = "signals"

    id:                   Mapped[int]            = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_id:          Mapped[int]            = mapped_column(ForeignKey("strategies.id"), nullable=False)
    asset_id:             Mapped[int]            = mapped_column(ForeignKey("assets.id"), nullable=False)
    signal_type:          Mapped[str]            = mapped_column(String(20), nullable=False)
    timestamp:            Mapped[datetime]       = mapped_column(DateTime, nullable=False)
    price:                Mapped[float]          = mapped_column(Float, nullable=False)
    confidence:           Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    regime:               Mapped[Optional[str]]  = mapped_column(String(30), nullable=True)
    timeframe_alignment:  Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    indicator_values:     Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    ai_prediction:        Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    sentiment_score:      Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    microstructure_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status:               Mapped[str]            = mapped_column(String(20), default="pending")
    rejection_reason:     Mapped[Optional[str]]  = mapped_column(Text, nullable=True)
    created_at:           Mapped[datetime]       = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_signal_strategy_ts", "strategy_id", "timestamp"),
    )

    strategy: Mapped["Strategy"]     = relationship("Strategy", back_populates="signals")
    asset:    Mapped["Asset"]        = relationship("Asset",    back_populates="signals")
    trades:   Mapped[list["Trade"]]  = relationship("Trade",    back_populates="signal")


# ── Trades ───────────────────────────────────────────────────
class Trade(Base):
    __tablename__ = "trades"

    id:          Mapped[int]              = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_id: Mapped[Optional[int]]    = mapped_column(ForeignKey("strategies.id"), nullable=True)
    signal_id:   Mapped[Optional[int]]    = mapped_column(ForeignKey("signals.id"), nullable=True)
    asset_id:    Mapped[int]              = mapped_column(ForeignKey("assets.id"), nullable=False)
    exchange_id: Mapped[int]              = mapped_column(ForeignKey("exchanges.id"), nullable=False)
    trade_type:  Mapped[str]              = mapped_column(String(20), nullable=False)
    side:        Mapped[str]              = mapped_column(String(5), nullable=False)
    entry_time:  Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    exit_time:   Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    entry_price: Mapped[float]            = mapped_column(Float, nullable=False)
    exit_price:  Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    quantity:    Mapped[float]            = mapped_column(Float, nullable=False)
    pnl:         Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    pnl_pct:     Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    fees:        Mapped[float]            = mapped_column(Float, default=0.0)
    slippage:    Mapped[float]            = mapped_column(Float, default=0.0)
    exit_reason: Mapped[Optional[str]]    = mapped_column(String(50), nullable=True)
    explanation: Mapped[Optional[dict]]   = mapped_column(JSONB, nullable=True)
    created_at:  Mapped[datetime]         = mapped_column(DateTime, default=datetime.utcnow)

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

    id:                Mapped[int]              = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id:          Mapped[Optional[int]]    = mapped_column(ForeignKey("trades.id"), nullable=True)
    exchange_id:       Mapped[int]              = mapped_column(ForeignKey("exchanges.id"), nullable=False)
    exchange_order_id: Mapped[Optional[str]]    = mapped_column(String(100), nullable=True)
    symbol:            Mapped[str]              = mapped_column(String(20), nullable=False)
    order_type:        Mapped[str]              = mapped_column(String(20), nullable=False)
    side:              Mapped[str]              = mapped_column(String(5), nullable=False)
    price:             Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    amount:            Mapped[float]            = mapped_column(Float, nullable=False)
    filled:            Mapped[float]            = mapped_column(Float, default=0.0)
    remaining:         Mapped[float]            = mapped_column(Float, default=0.0)
    status:            Mapped[str]              = mapped_column(String(20), default="open")
    timestamp:         Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at:        Mapped[datetime]         = mapped_column(DateTime, default=datetime.utcnow)

    trade:    Mapped[Optional["Trade"]] = relationship("Trade",    back_populates="orders")
    exchange: Mapped["Exchange"]        = relationship("Exchange", back_populates="orders")


# ── Positions ────────────────────────────────────────────────
class Position(Base):
    __tablename__ = "positions"

    id:             Mapped[int]              = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_id:    Mapped[Optional[int]]    = mapped_column(ForeignKey("strategies.id"), nullable=True)
    exchange_id:    Mapped[int]              = mapped_column(ForeignKey("exchanges.id"), nullable=False)
    asset_id:       Mapped[int]              = mapped_column(ForeignKey("assets.id"), nullable=False)
    position_type:  Mapped[str]              = mapped_column(String(10), nullable=False)
    side:           Mapped[str]              = mapped_column(String(5), nullable=False)
    entry_price:    Mapped[float]            = mapped_column(Float, nullable=False)
    current_price:  Mapped[float]            = mapped_column(Float, default=0.0)
    quantity:       Mapped[float]            = mapped_column(Float, nullable=False)
    unrealized_pnl: Mapped[float]            = mapped_column(Float, default=0.0)
    stop_loss:      Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    take_profit:    Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    is_open:        Mapped[bool]             = mapped_column(Boolean, default=True)
    opened_at:      Mapped[datetime]         = mapped_column(DateTime, default=datetime.utcnow)
    updated_at:     Mapped[datetime]         = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    strategy: Mapped[Optional["Strategy"]] = relationship("Strategy", back_populates="positions")
    exchange: Mapped["Exchange"]           = relationship("Exchange",  back_populates="positions")
    asset:    Mapped["Asset"]              = relationship("Asset",     back_populates="positions")


# ── Sentiment Data ───────────────────────────────────────────
class SentimentData(Base):
    __tablename__ = "sentiment_data"

    id:              Mapped[int]              = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id:        Mapped[Optional[int]]    = mapped_column(ForeignKey("assets.id"), nullable=True)
    source:          Mapped[str]              = mapped_column(String(20), nullable=False)
    timestamp:       Mapped[datetime]         = mapped_column(DateTime, nullable=False)
    sentiment_score: Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    narrative_score: Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    attention_index: Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    raw_data:        Mapped[Optional[dict]]   = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        Index("idx_sentiment_asset_ts", "asset_id", "timestamp"),
    )


# ── ML Models Registry ───────────────────────────────────────
class MLModel(Base):
    __tablename__ = "ml_models"

    id:                 Mapped[int]              = mapped_column(Integer, primary_key=True, autoincrement=True)
    name:               Mapped[str]              = mapped_column(String(200), nullable=False)
    model_type:         Mapped[str]              = mapped_column(String(50), nullable=False)
    asset_id:           Mapped[Optional[int]]    = mapped_column(ForeignKey("assets.id"), nullable=True)
    timeframe:          Mapped[Optional[str]]    = mapped_column(String(5), nullable=True)
    version:            Mapped[int]              = mapped_column(Integer, default=1)
    accuracy:           Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    feature_importance: Mapped[Optional[dict]]   = mapped_column(JSONB, nullable=True)
    model_path:         Mapped[Optional[str]]    = mapped_column(String(500), nullable=True)
    trained_at:         Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    valid_until:        Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    is_active:          Mapped[bool]             = mapped_column(Boolean, default=False)

    predictions: Mapped[list["ModelPrediction"]] = relationship("ModelPrediction", back_populates="model", cascade="all, delete-orphan")


# ── Model Predictions ────────────────────────────────────────
class ModelPrediction(Base):
    __tablename__ = "model_predictions"

    id:                  Mapped[int]              = mapped_column(Integer, primary_key=True, autoincrement=True)
    model_id:            Mapped[int]              = mapped_column(ForeignKey("ml_models.id"), nullable=False)
    asset_id:            Mapped[int]              = mapped_column(ForeignKey("assets.id"), nullable=False)
    timestamp:           Mapped[datetime]         = mapped_column(DateTime, nullable=False)
    bullish_probability: Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    bearish_probability: Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    expected_return:     Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    confidence:          Mapped[Optional[float]]  = mapped_column(Float, nullable=True)

    __table_args__ = (
        Index("idx_prediction_model_ts", "model_id", "timestamp"),
    )

    model: Mapped["MLModel"] = relationship("MLModel", back_populates="predictions")


# ── Market Regimes ───────────────────────────────────────────
class MarketRegime(Base):
    __tablename__ = "market_regimes"

    id:         Mapped[int]              = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id:   Mapped[int]              = mapped_column(ForeignKey("assets.id"), nullable=False)
    timeframe:  Mapped[str]              = mapped_column(String(5), nullable=False)
    timestamp:  Mapped[datetime]         = mapped_column(DateTime, nullable=False)
    regime:     Mapped[str]              = mapped_column(String(30), nullable=False)
    confidence: Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    features:   Mapped[Optional[dict]]   = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        Index("idx_regime_asset_tf_ts", "asset_id", "timeframe", "timestamp"),
    )


# ── Portfolio Snapshots ──────────────────────────────────────
class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"

    id:              Mapped[int]            = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_type:   Mapped[str]            = mapped_column(String(10), nullable=False)
    timestamp:       Mapped[datetime]       = mapped_column(DateTime, default=datetime.utcnow)
    total_value:     Mapped[float]          = mapped_column(Float, default=0.0)
    cash_balance:    Mapped[float]          = mapped_column(Float, default=0.0)
    positions_value: Mapped[float]          = mapped_column(Float, default=0.0)
    unrealized_pnl:  Mapped[float]          = mapped_column(Float, default=0.0)
    daily_pnl:       Mapped[float]          = mapped_column(Float, default=0.0)
    drawdown:        Mapped[float]          = mapped_column(Float, default=0.0)
    holdings:        Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        Index("idx_portfolio_type_ts", "snapshot_type", "timestamp"),
    )


# ── Backtest Results ─────────────────────────────────────────
class BacktestResult(Base):
    __tablename__ = "backtest_results"

    id:               Mapped[int]              = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_id:      Mapped[Optional[int]]    = mapped_column(ForeignKey("strategies.id"), nullable=True)
    strategy_name:    Mapped[str]              = mapped_column(String(200), nullable=False)
    symbol:           Mapped[str]              = mapped_column(String(20), nullable=False)
    timeframe:        Mapped[str]              = mapped_column(String(5), nullable=False)
    initial_capital:  Mapped[float]            = mapped_column(Float, default=10000.0)
    final_capital:    Mapped[float]            = mapped_column(Float, default=0.0)
    total_return_pct: Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    max_drawdown_pct: Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    sharpe_ratio:     Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    win_rate:         Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    total_trades:     Mapped[int]              = mapped_column(Integer, default=0)
    profit_factor:    Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    run_config:       Mapped[Optional[dict]]   = mapped_column(JSONB, nullable=True)
    equity_curve:     Mapped[Optional[list]]   = mapped_column(JSONB, nullable=True)
    trade_log:        Mapped[Optional[list]]   = mapped_column(JSONB, nullable=True)
    created_at:       Mapped[datetime]         = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_backtest_strategy_ts", "strategy_id", "created_at"),
    )


# ── Paper Trades (IDSS Paper Executor) ───────────────────────
class PaperTrade(Base):
    __tablename__ = "paper_trades"

    id:              Mapped[int]              = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol:          Mapped[str]              = mapped_column(String(30), nullable=False)
    side:            Mapped[str]              = mapped_column(String(5), nullable=False)
    regime:          Mapped[str]              = mapped_column(String(40), default="")
    timeframe:       Mapped[str]              = mapped_column(String(10), default="")
    entry_price:     Mapped[float]            = mapped_column(Float, nullable=False)
    exit_price:      Mapped[float]            = mapped_column(Float, nullable=False)
    stop_loss:       Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    take_profit:     Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    size_usdt:       Mapped[float]            = mapped_column(Float, nullable=False)
    entry_size_usdt: Mapped[Optional[float]]  = mapped_column(Float, nullable=True, default=None)
    exit_size_usdt:  Mapped[Optional[float]]  = mapped_column(Float, nullable=True, default=None)
    pnl_usdt:        Mapped[float]            = mapped_column(Float, nullable=False)
    pnl_pct:         Mapped[float]            = mapped_column(Float, nullable=False)
    score:           Mapped[float]            = mapped_column(Float, default=0.0)
    exit_reason:     Mapped[str]              = mapped_column(String(30), default="")
    models_fired:    Mapped[Optional[list]]   = mapped_column(JSONB, nullable=True)
    rationale:       Mapped[Optional[str]]    = mapped_column(Text, nullable=True)
    duration_s:      Mapped[int]              = mapped_column(Integer, default=0)
    opened_at:       Mapped[str]              = mapped_column(String(40), nullable=False)
    closed_at:       Mapped[str]              = mapped_column(String(40), nullable=False)
    created_at:      Mapped[datetime]         = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_paper_trades_symbol", "symbol"),
        Index("idx_paper_trades_closed_at", "closed_at"),
    )

    def to_dict(self) -> dict:
        _esz = self.entry_size_usdt if self.entry_size_usdt is not None else self.size_usdt
        _xsz = self.exit_size_usdt if self.exit_size_usdt is not None else self.size_usdt
        return {
            "symbol": self.symbol, "side": self.side, "regime": self.regime,
            "timeframe": self.timeframe, "entry_price": self.entry_price,
            "exit_price": self.exit_price, "stop_loss": self.stop_loss,
            "take_profit": self.take_profit, "size_usdt": self.size_usdt,
            "entry_size_usdt": _esz, "exit_size_usdt": _xsz,
            "pnl_usdt": self.pnl_usdt, "pnl_pct": self.pnl_pct,
            "score": self.score, "exit_reason": self.exit_reason,
            "models_fired": self.models_fired or [], "rationale": self.rationale or "",
            "duration_s": self.duration_s, "opened_at": self.opened_at,
            "closed_at": self.closed_at,
        }


# ── Live Trades ──────────────────────────────────────────────
class LiveTrade(Base):
    __tablename__ = "live_trades"

    id:             Mapped[int]              = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol:         Mapped[str]              = mapped_column(String(30), nullable=False)
    side:           Mapped[str]              = mapped_column(String(5), nullable=False)
    status:         Mapped[str]              = mapped_column(String(10), default="open")
    regime:         Mapped[str]              = mapped_column(String(40), default="")
    timeframe:      Mapped[str]              = mapped_column(String(10), default="")
    entry_price:    Mapped[float]            = mapped_column(Float, nullable=False)
    exit_price:     Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    stop_loss:      Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    take_profit:    Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    size_usdt:      Mapped[float]            = mapped_column(Float, nullable=False)
    pnl_usdt:       Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    pnl_pct:        Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    score:          Mapped[float]            = mapped_column(Float, default=0.0)
    exit_reason:    Mapped[str]              = mapped_column(String(30), default="")
    models_fired:   Mapped[Optional[list]]   = mapped_column(JSONB, nullable=True)
    rationale:      Mapped[Optional[str]]    = mapped_column(Text, nullable=True)
    entry_order_id: Mapped[str]              = mapped_column(String(80), default="")
    exit_order_id:  Mapped[str]              = mapped_column(String(80), default="")
    duration_s:     Mapped[int]              = mapped_column(Integer, default=0)
    opened_at:      Mapped[str]              = mapped_column(String(40), nullable=False)
    closed_at:      Mapped[str]              = mapped_column(String(40), default="")
    created_at:     Mapped[datetime]         = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_live_trades_symbol", "symbol"),
        Index("idx_live_trades_status", "status"),
        Index("idx_live_trades_closed_at", "closed_at"),
    )

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol, "side": self.side, "status": self.status,
            "regime": self.regime, "timeframe": self.timeframe,
            "entry_price": self.entry_price, "exit_price": self.exit_price,
            "stop_loss": self.stop_loss, "take_profit": self.take_profit,
            "size_usdt": self.size_usdt, "pnl_usdt": self.pnl_usdt,
            "pnl_pct": self.pnl_pct, "score": self.score,
            "exit_reason": self.exit_reason, "models_fired": self.models_fired or [],
            "rationale": self.rationale or "", "entry_order_id": self.entry_order_id,
            "exit_order_id": self.exit_order_id, "duration_s": self.duration_s,
            "opened_at": self.opened_at, "closed_at": self.closed_at,
        }


# ── Settings ─────────────────────────────────────────────────
class Setting(Base):
    __tablename__ = "settings"

    key:        Mapped[str]            = mapped_column(String(200), primary_key=True)
    value:      Mapped[Optional[str]]  = mapped_column(Text, nullable=True)
    category:   Mapped[Optional[str]]  = mapped_column(String(50), nullable=True)
    updated_at: Mapped[datetime]       = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ── System Logs ──────────────────────────────────────────────
class SystemLog(Base):
    __tablename__ = "system_logs"

    id:        Mapped[int]            = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime]       = mapped_column(DateTime, default=datetime.utcnow)
    level:     Mapped[str]            = mapped_column(String(10), nullable=False)
    module:    Mapped[str]            = mapped_column(String(100), nullable=False)
    message:   Mapped[str]            = mapped_column(Text, nullable=False)
    details:   Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        Index("idx_log_level_ts", "level", "timestamp"),
    )


# ── Agent Signals ────────────────────────────────────────────
class AgentSignal(Base):
    __tablename__ = "agent_signals"

    id:                Mapped[int]              = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_name:        Mapped[str]              = mapped_column(String(50), nullable=False, index=True)
    timestamp:         Mapped[datetime]         = mapped_column(DateTime, nullable=False, index=True)
    signal:            Mapped[float]            = mapped_column(Float, nullable=False)
    confidence:        Mapped[float]            = mapped_column(Float, nullable=False)
    is_stale:          Mapped[bool]             = mapped_column(Boolean, default=False)
    symbol:            Mapped[Optional[str]]    = mapped_column(String(20), nullable=True)
    topic:             Mapped[str]              = mapped_column(String(100), nullable=False)
    payload:           Mapped[Optional[dict]]   = mapped_column(JSONB, nullable=True)
    regime_bias:       Mapped[Optional[str]]    = mapped_column(String(30), nullable=True)
    macro_risk_score:  Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    macro_veto:        Mapped[Optional[bool]]   = mapped_column(Boolean, nullable=True)

    __table_args__ = (
        Index("idx_agent_signals_agent_ts", "agent_name", "timestamp"),
        Index("idx_agent_signals_symbol_ts", "symbol", "timestamp"),
    )


# ── Signal Log ───────────────────────────────────────────────
class SignalLog(Base):
    __tablename__ = "signal_log"

    id:               Mapped[int]            = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp:        Mapped[datetime]       = mapped_column(DateTime, nullable=False, index=True)
    symbol:           Mapped[str]            = mapped_column(String(30), nullable=False, index=True)
    strategy_name:    Mapped[str]            = mapped_column(String(80), nullable=False, index=True)
    direction:        Mapped[str]            = mapped_column(String(10), nullable=False)
    strength:         Mapped[float]          = mapped_column(Float, nullable=False)
    entry_price:      Mapped[float]          = mapped_column(Float, nullable=False)
    stop_loss:        Mapped[float]          = mapped_column(Float, nullable=False)
    take_profit:      Mapped[float]          = mapped_column(Float, nullable=False)
    regime:           Mapped[Optional[str]]  = mapped_column(String(40), nullable=True)
    timeframe:        Mapped[Optional[str]]  = mapped_column(String(10), nullable=True)
    rationale:        Mapped[Optional[str]]  = mapped_column(Text, nullable=True)
    models_fired:     Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    approved:         Mapped[bool]           = mapped_column(Boolean, default=False)
    rejection_reason: Mapped[Optional[str]]  = mapped_column(String(200), nullable=True)

    __table_args__ = (
        Index("idx_signal_log_symbol_ts", "symbol", "timestamp"),
        Index("idx_signal_log_strategy", "strategy_name", "timestamp"),
    )


# ── Trade Feedback ───────────────────────────────────────────
class TradeFeedback(Base):
    __tablename__ = "trade_feedback"

    id:               Mapped[int]            = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id:         Mapped[str]            = mapped_column(String(100), nullable=False, unique=True)
    symbol:           Mapped[str]            = mapped_column(String(30), nullable=False)
    side:             Mapped[str]            = mapped_column(String(5), nullable=False)
    regime:           Mapped[str]            = mapped_column(String(40), default="")
    models_fired:     Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    setup_score:      Mapped[float]          = mapped_column(Float, default=0.0)
    risk_score:       Mapped[float]          = mapped_column(Float, default=0.0)
    execution_score:  Mapped[float]          = mapped_column(Float, default=0.0)
    decision_score:   Mapped[float]          = mapped_column(Float, default=0.0)
    overall_score:    Mapped[float]          = mapped_column(Float, default=0.0)
    classification:   Mapped[str]            = mapped_column(String(10), default="NEUTRAL")
    hard_overrides:   Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    root_causes:      Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    recommendations:  Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    penalty_log:      Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    ai_explanation:   Mapped[Optional[str]]  = mapped_column(Text, nullable=True)
    pnl_usdt:         Mapped[float]          = mapped_column(Float, default=0.0)
    pnl_pct:          Mapped[float]          = mapped_column(Float, default=0.0)
    exit_reason:      Mapped[str]            = mapped_column(String(30), default="")
    duration_s:       Mapped[int]            = mapped_column(Integer, default=0)
    created_at:       Mapped[datetime]       = mapped_column(DateTime, default=datetime.utcnow)
    # Phase 2 forensics
    decision_outcome_matrix:    Mapped[Optional[str]]  = mapped_column(String(40), nullable=True)
    avoidable_loss_flag:        Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    avoidable_win_flag:         Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    was_loss_acceptable:        Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    failure_domain_primary:     Mapped[Optional[str]]  = mapped_column(String(30), nullable=True)
    failure_domain_secondary:   Mapped[Optional[str]]  = mapped_column(String(30), nullable=True)
    preventability_score:       Mapped[float]          = mapped_column(Float, default=0.0)
    randomness_score:           Mapped[float]          = mapped_column(Float, default=0.0)
    model_conflict_score:       Mapped[float]          = mapped_column(Float, default=0.0)
    regime_confidence_at_entry: Mapped[float]          = mapped_column(Float, default=0.0)
    htf_confirmed_at_entry:     Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    signal_conflict_score:      Mapped[float]          = mapped_column(Float, default=0.0)

    __table_args__ = (
        Index("idx_feedback_symbol", "symbol"),
        Index("idx_feedback_regime", "regime"),
        Index("idx_feedback_classification", "classification"),
        Index("idx_feedback_created_at", "created_at"),
    )


# ── Strategy Tuning Proposals ────────────────────────────────
class StrategyTuningProposal(Base):
    __tablename__ = "strategy_tuning_proposals"

    id:                         Mapped[int]              = mapped_column(Integer, primary_key=True, autoincrement=True)
    proposal_id:                Mapped[str]              = mapped_column(String(30), nullable=False, unique=True)
    root_cause_category:        Mapped[str]              = mapped_column(String(50), nullable=False)
    rec_id:                     Mapped[str]              = mapped_column(String(80), nullable=False)
    trigger_evidence:           Mapped[Optional[dict]]   = mapped_column(JSONB, nullable=True)
    affected_subsystem:         Mapped[str]              = mapped_column(String(100), default="")
    tuning_parameter:           Mapped[str]              = mapped_column(String(100), default="")
    tuning_direction:           Mapped[str]              = mapped_column(String(20), default="manual")
    proposed_change_description: Mapped[str]             = mapped_column(Text, default="")
    expected_benefit:           Mapped[str]              = mapped_column(Text, default="")
    confidence:                 Mapped[float]            = mapped_column(Float, default=0.0)
    risk_level:                 Mapped[str]              = mapped_column(String(10), default="medium")
    auto_tune_eligible:         Mapped[bool]             = mapped_column(Boolean, default=False)
    requires_manual_approval:   Mapped[bool]             = mapped_column(Boolean, default=True)
    status:                     Mapped[str]              = mapped_column(String(20), default="pending")
    backtest_result:            Mapped[Optional[dict]]   = mapped_column(JSONB, nullable=True)
    promoted_at:                Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    rejected_at:                Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    rejection_reason:           Mapped[Optional[str]]    = mapped_column(Text, nullable=True)
    created_at:                 Mapped[datetime]         = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_proposal_status", "status"),
        Index("idx_proposal_root_cause", "root_cause_category"),
        Index("idx_proposal_param", "tuning_parameter"),
    )


# ── Applied Strategy Changes ─────────────────────────────────
class AppliedStrategyChange(Base):
    __tablename__ = "applied_strategy_changes"

    id:                    Mapped[int]      = mapped_column(Integer, primary_key=True, autoincrement=True)
    proposal_id:           Mapped[str]      = mapped_column(String(30), nullable=False)
    root_cause_category:   Mapped[str]      = mapped_column(String(50), default="")
    tuning_parameter:      Mapped[str]      = mapped_column(String(100), default="")
    tuning_direction:      Mapped[str]      = mapped_column(String(20), default="manual")
    applied_value:         Mapped[str]      = mapped_column(Text, default="")
    applied_by:            Mapped[str]      = mapped_column(String(20), default="auto")
    notes:                 Mapped[str]      = mapped_column(Text, default="")
    backtest_delta_pf_pct: Mapped[float]    = mapped_column(Float, default=0.0)
    applied_at:            Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_applied_param", "tuning_parameter"),
        Index("idx_applied_applied_at", "applied_at"),
    )


# ── Tuning Proposal Outcomes ─────────────────────────────────
class TuningProposalOutcome(Base):
    __tablename__ = "tuning_proposal_outcomes"

    id:                   Mapped[int]              = mapped_column(Integer, primary_key=True, autoincrement=True)
    proposal_id:          Mapped[str]              = mapped_column(String(30), nullable=False, unique=True)
    status:               Mapped[str]              = mapped_column(String(30), default="PENDING_EVALUATION")
    min_trades_threshold: Mapped[int]              = mapped_column(Integer, default=30)
    pre_trades:           Mapped[int]              = mapped_column(Integer, default=0)
    pre_win_rate:         Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    pre_profit_factor:    Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    pre_avg_r:            Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    post_trades:          Mapped[int]              = mapped_column(Integer, default=0)
    post_win_rate:        Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    post_profit_factor:   Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    post_avg_r:           Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    delta_win_rate:       Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    delta_pf:             Mapped[Optional[float]]  = mapped_column(Float, nullable=True)
    verdict:              Mapped[Optional[str]]    = mapped_column(String(20), nullable=True)
    notes:                Mapped[Optional[str]]    = mapped_column(Text, nullable=True)
    applied_at:           Mapped[datetime]         = mapped_column(DateTime, default=datetime.utcnow)
    measured_at:          Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at:           Mapped[datetime]         = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_tpo_status", "status"),
        Index("idx_tpo_proposal_id", "proposal_id"),
    )
