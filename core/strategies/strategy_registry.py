# ============================================================
# NEXUS TRADER — Strategy Registry Definition
# ============================================================
# Central registry of all live auto-trading models and their
# configurable parameters. The Strategies page UI reads this
# registry to populate the model list, parameter editors, and
# live setting management.
#
# Generated: 2026-03-18
# ============================================================

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Optional
import logging

logger = logging.getLogger(__name__)


# ============================================================
# Data Models
# ============================================================

@dataclass
class ModelParamDef:
    """
    Definition of a single configurable parameter for a model.
    Maps to a dotted config key and provides UI rendering metadata.
    """

    key: str                           # dotted config key, e.g. "models.trend.adx_min"
    label: str                         # human-readable label, e.g. "ADX Minimum Threshold"
    param_type: str                    # "float", "int", "bool", "enum"
    default: Any                       # code-defined default value
    min_val: Optional[float] = None    # for numeric types
    max_val: Optional[float] = None
    step: Optional[float] = None       # spinbox/slider step
    description: str = ""              # tooltip text
    section: str = "General"           # grouping within detail panel
    enum_values: Optional[list[str]] = None  # for enum types

    def __post_init__(self):
        """Validate parameter definition."""
        if self.param_type not in ("float", "int", "bool", "enum"):
            raise ValueError(f"Invalid param_type: {self.param_type}")
        if self.param_type == "enum" and not self.enum_values:
            raise ValueError(f"enum type requires enum_values: {self.key}")
        if self.param_type in ("float", "int"):
            if self.min_val is not None and self.max_val is not None:
                if self.min_val >= self.max_val:
                    raise ValueError(f"min_val ({self.min_val}) >= max_val ({self.max_val}): {self.key}")


@dataclass
class ModelDef:
    """
    Complete definition of a trading model.
    Includes metadata, weights, and all configurable parameters.
    """

    name: str                          # internal name, e.g. "trend"
    display_name: str                  # UI name, e.g. "Trend Model"
    model_type: str                    # "CORE", "AGENT", "ML", "META"
    type_color: str                    # hex color for badge (e.g. "#4488CC")
    description: str                   # one-line description
    weight_key: str                    # config key for weight, e.g. "model_weights.trend"
    default_weight: float              # code-defined default weight
    params: list[ModelParamDef] = field(default_factory=list)
    enabled_by_default: bool = True    # if False, lives in disabled_models list

    def __post_init__(self):
        """Validate model definition."""
        if self.model_type not in ("CORE", "AGENT", "ML", "META"):
            raise ValueError(f"Invalid model_type: {self.model_type}")
        if not 0.0 <= self.default_weight <= 1.0:
            raise ValueError(f"default_weight must be in [0.0, 1.0], got {self.default_weight}")
        if not self.type_color.startswith("#"):
            raise ValueError(f"type_color must be hex (e.g. #4488CC), got {self.type_color}")


# ============================================================
# Helper Functions (Module-Level)
# ============================================================

def _p(key: str, label: str, ptype: str, default: Any,
       min_val: Optional[float] = None, max_val: Optional[float] = None,
       step: Optional[float] = None, desc: str = "",
       section: str = "General", enum_vals: Optional[list[str]] = None) -> ModelParamDef:
    """Shorthand factory for creating ModelParamDef objects."""
    return ModelParamDef(
        key=key, label=label, param_type=ptype, default=default,
        min_val=min_val, max_val=max_val, step=step,
        description=desc, section=section, enum_values=enum_vals
    )


# ============================================================
# Model Definitions
# ============================================================

# CORE MODELS (Blue #4488CC)
# ────────────────────────────────────────────────────────

TREND_MODEL = ModelDef(
    name="trend",
    display_name="Trend Model",
    model_type="CORE",
    type_color="#4488CC",
    description="Trend-following entries in directional regimes (bull/bear).",
    weight_key="model_weights.trend",
    default_weight=0.35,
    enabled_by_default=True,
    params=[
        # Entry parameters
        _p("models.trend.entry_buffer_atr", "Entry Buffer ATR", "float", 0.20,
           min_val=-0.50, max_val=0.50, step=0.01,
           desc="ATR multiplier for entry price offset (limit orders). Positive = pay up to confirm breakout.",
           section="Entry"),
        _p("models.trend.adx_min", "ADX Minimum", "float", 25.0,
           min_val=10.0, max_val=50.0, step=1.0,
           desc="Minimum ADX14 value to confirm trend strength.",
           section="Entry"),
        _p("models.trend.rsi_long_min", "RSI Long Min", "float", 45.0,
           min_val=20.0, max_val=60.0, step=1.0,
           desc="Minimum RSI14 for long entries (oversold filter).",
           section="Entry"),
        _p("models.trend.rsi_long_max", "RSI Long Max", "float", 70.0,
           min_val=60.0, max_val=90.0, step=1.0,
           desc="Maximum RSI14 for long entries (overbought limit).",
           section="Entry"),
        _p("models.trend.rsi_short_min", "RSI Short Min", "float", 30.0,
           min_val=10.0, max_val=40.0, step=1.0,
           desc="Minimum RSI14 for short entries.",
           section="Entry"),
        _p("models.trend.rsi_short_max", "RSI Short Max", "float", 55.0,
           min_val=40.0, max_val=70.0, step=1.0,
           desc="Maximum RSI14 for short entries.",
           section="Entry"),
        # Signal strength parameters
        _p("models.trend.strength_base", "Base Strength", "float", 0.15,
           min_val=0.05, max_val=0.40, step=0.01,
           desc="Base signal strength before bonuses.",
           section="Signal Strength"),
        _p("models.trend.ema20_bonus", "EMA20 Bonus", "float", 0.15,
           min_val=0.0, max_val=0.50, step=0.01,
           desc="Strength bonus when EMA20 trend aligns.",
           section="Signal Strength"),
        _p("models.trend.macd_bonus", "MACD Bonus", "float", 0.20,
           min_val=0.0, max_val=0.50, step=0.01,
           desc="Strength bonus when MACD confirms direction.",
           section="Signal Strength"),
        _p("models.trend.adx_bonus_max", "ADX Bonus Max", "float", 0.30,
           min_val=0.0, max_val=0.60, step=0.01,
           desc="Maximum bonus from high ADX strength.",
           section="Signal Strength"),
    ]
)

MOMENTUM_BREAKOUT_MODEL = ModelDef(
    name="momentum_breakout",
    display_name="Momentum Breakout Model",
    model_type="CORE",
    type_color="#4488CC",
    description="Breakout-following entries after volume expansion.",
    weight_key="model_weights.momentum_breakout",
    default_weight=0.25,
    enabled_by_default=True,
    params=[
        _p("models.momentum_breakout.entry_buffer_atr", "Entry Buffer ATR", "float", 0.10,
           min_val=-0.50, max_val=0.50, step=0.01,
           desc="ATR multiplier for entry offset.",
           section="Entry"),
        _p("models.momentum_breakout.lookback", "Lookback Period", "int", 20,
           min_val=10, max_val=50, step=1,
           desc="Bars to check for breakout (high/low).",
           section="Entry"),
        _p("models.momentum_breakout.vol_mult_min", "Volume Multiplier Min", "float", 1.5,
           min_val=1.0, max_val=3.0, step=0.1,
           desc="Minimum volume spike multiplier vs average.",
           section="Entry"),
        _p("models.momentum_breakout.rsi_bullish", "RSI Bullish Threshold", "float", 55.0,
           min_val=50.0, max_val=70.0, step=1.0,
           desc="Minimum RSI14 for long breakouts.",
           section="Entry"),
        _p("models.momentum_breakout.rsi_bearish", "RSI Bearish Threshold", "float", 45.0,
           min_val=30.0, max_val=50.0, step=1.0,
           desc="Maximum RSI14 for short breakouts.",
           section="Entry"),
        _p("models.momentum_breakout.strength_base", "Base Strength", "float", 0.35,
           min_val=0.10, max_val=0.60, step=0.01,
           desc="Base signal strength (high for momentum entries).",
           section="Signal Strength"),
    ]
)

VWAP_REVERSION_MODEL = ModelDef(
    name="vwap_reversion",
    display_name="VWAP Reversion Model",
    model_type="CORE",
    type_color="#4488CC",
    description="Mean reversion toward volume-weighted average price.",
    weight_key="model_weights.vwap_reversion",
    default_weight=0.28,
    enabled_by_default=True,
    params=[
        _p("models.vwap_reversion.entry_buffer_atr", "Entry Buffer ATR", "float", -0.10,
           min_val=-0.50, max_val=0.50, step=0.01,
           desc="Negative = wait for better fill on reversion entry.",
           section="Entry"),
        _p("models.vwap_reversion.z_threshold", "Z-Score Threshold", "float", 1.5,
           min_val=0.5, max_val=3.0, step=0.1,
           desc="Standard deviations from VWAP to trigger reversion.",
           section="Entry"),
        _p("models.vwap_reversion.rsi_oversold", "RSI Oversold", "float", 42.0,
           min_val=25.0, max_val=45.0, step=1.0,
           desc="RSI14 floor for long reversal entries.",
           section="Entry"),
        _p("models.vwap_reversion.rsi_overbought", "RSI Overbought", "float", 58.0,
           min_val=55.0, max_val=75.0, step=1.0,
           desc="RSI14 ceiling for short reversal entries.",
           section="Entry"),
        _p("models.vwap_reversion.deviation_window", "Deviation Window", "int", 20,
           min_val=10, max_val=50, step=1,
           desc="Bars for rolling VWAP std deviation.",
           section="Entry"),
        _p("models.vwap_reversion.sl_atr_mult", "Stop-Loss ATR Mult", "float", 1.2,
           min_val=0.5, max_val=3.0, step=0.1,
           desc="ATR multiplier for stop-loss distance.",
           section="Exit"),
        _p("models.vwap_reversion.tp_atr_offset", "Take-Profit ATR Offset", "float", 0.5,
           min_val=0.2, max_val=2.0, step=0.1,
           desc="ATR multiplier for take-profit distance.",
           section="Exit"),
    ]
)

MEAN_REVERSION_MODEL = ModelDef(
    name="mean_reversion",
    display_name="Mean Reversion Model",
    model_type="CORE",
    type_color="#4488CC",
    description="Bollinger Bands reversion in ranging regimes.",
    weight_key="model_weights.mean_reversion",
    default_weight=0.25,
    enabled_by_default=False,  # DISABLED by default per backtest results
    params=[
        _p("models.mean_reversion.entry_buffer_atr", "Entry Buffer ATR", "float", -0.15,
           min_val=-0.50, max_val=0.50, step=0.01,
           desc="Negative = wait for better fill closer to midline.",
           section="Entry"),
        _p("models.mean_reversion.bb_lower_dist", "BB Lower Distance", "float", 0.15,
           min_val=0.05, max_val=0.40, step=0.01,
           desc="Distance from BB lower as % of range (trigger zone).",
           section="Entry"),
        _p("models.mean_reversion.rsi_oversold", "RSI Oversold", "float", 35.0,
           min_val=20.0, max_val=40.0, step=1.0,
           desc="RSI14 floor for long reversal.",
           section="Entry"),
        _p("models.mean_reversion.rsi_overbought", "RSI Overbought", "float", 65.0,
           min_val=60.0, max_val=80.0, step=1.0,
           desc="RSI14 ceiling for short reversal.",
           section="Entry"),
        _p("models.mean_reversion.stoch_rsi_oversold", "Stoch RSI Oversold", "float", 25.0,
           min_val=10.0, max_val=40.0, step=1.0,
           desc="Stochastic RSI(14,14,3,3) oversold level.",
           section="Entry"),
        _p("models.mean_reversion.stoch_rsi_overbought", "Stoch RSI Overbought", "float", 75.0,
           min_val=60.0, max_val=90.0, step=1.0,
           desc="Stochastic RSI(14,14,3,3) overbought level.",
           section="Entry"),
    ]
)

LIQUIDITY_SWEEP_MODEL = ModelDef(
    name="liquidity_sweep",
    display_name="Liquidity Sweep Model",
    model_type="CORE",
    type_color="#4488CC",
    description="Reversal after liquidity sweep and stop-hunt.",
    weight_key="model_weights.liquidity_sweep",
    default_weight=0.15,
    enabled_by_default=False,  # DISABLED by default per backtest results
    params=[
        _p("models.liquidity_sweep.swing_lookback", "Swing Lookback", "int", 15,
           min_val=5, max_val=30, step=1,
           desc="Bars for recent swing high/low.",
           section="Entry"),
        _p("models.liquidity_sweep.min_sweep_pct", "Min Sweep %", "float", 0.10,
           min_val=0.05, max_val=0.50, step=0.01,
           desc="Minimum sweep depth as % of price (trigger zone).",
           section="Entry"),
        _p("models.liquidity_sweep.vol_mult_min", "Volume Multiplier Min", "float", 1.3,
           min_val=1.0, max_val=2.5, step=0.1,
           desc="Minimum volume spike during sweep.",
           section="Entry"),
        _p("models.liquidity_sweep.cascade_risk_cutoff", "Cascade Risk Cutoff", "float", 0.70,
           min_val=0.40, max_val=0.95, step=0.05,
           desc="Suppress signal if liquidation cascade risk above this.",
           section="Risk"),
        _p("models.liquidity_sweep.liq_density_threshold", "Liquidity Density", "float", 0.60,
           min_val=0.30, max_val=0.90, step=0.05,
           desc="Order book imbalance trigger threshold.",
           section="Risk"),
    ]
)


# AGENT MODELS (Orange #CC7700)
# ────────────────────────────────────────────────────────

FUNDING_RATE_MODEL = ModelDef(
    name="funding_rate",
    display_name="Funding Rate Model",
    model_type="AGENT",
    type_color="#CC7700",
    description="Contrarian signal from perpetual funding rates.",
    weight_key="model_weights.funding_rate",
    default_weight=0.20,
    enabled_by_default=True,
    params=[
        _p("models.funding_rate.min_signal", "Min Signal Strength", "float", 0.40,
           min_val=0.10, max_val=0.80, step=0.05,
           desc="Minimum |signal_val| (0-1) to fire.",
           section="Entry"),
        _p("models.funding_rate.min_confidence", "Min Confidence", "float", 0.55,
           min_val=0.30, max_val=0.90, step=0.05,
           desc="Minimum confidence score (0-1).",
           section="Entry"),
        _p("models.funding_rate.sl_atr_mult", "Stop-Loss ATR Mult", "float", 1.5,
           min_val=0.5, max_val=3.0, step=0.1,
           desc="ATR multiplier for stop-loss.",
           section="Exit"),
        _p("models.funding_rate.tp_atr_mult", "Take-Profit ATR Mult", "float", 2.5,
           min_val=1.0, max_val=5.0, step=0.1,
           desc="ATR multiplier for take-profit.",
           section="Exit"),
    ]
)

ORDER_BOOK_MODEL = ModelDef(
    name="order_book",
    display_name="Order Book Model",
    model_type="AGENT",
    type_color="#CC7700",
    description="Microstructure imbalance signal from order book depth.",
    weight_key="model_weights.order_book",
    default_weight=0.18,
    enabled_by_default=True,
    params=[
        _p("models.order_book.min_signal", "Min Signal Strength", "float", 0.35,
           min_val=0.10, max_val=0.80, step=0.05,
           desc="Minimum |signal_val| (0-1) to fire.",
           section="Entry"),
        _p("models.order_book.min_confidence", "Min Confidence", "float", 0.60,
           min_val=0.30, max_val=0.90, step=0.05,
           desc="Minimum confidence score (0-1).",
           section="Entry"),
        _p("models.order_book.sl_atr_mult", "Stop-Loss ATR Mult", "float", 1.5,
           min_val=0.5, max_val=3.0, step=0.1,
           desc="ATR multiplier for stop-loss.",
           section="Exit"),
        _p("models.order_book.tp_atr_mult", "Take-Profit ATR Mult", "float", 2.0,
           min_val=1.0, max_val=5.0, step=0.1,
           desc="ATR multiplier for take-profit.",
           section="Exit"),
    ]
)

SENTIMENT_MODEL = ModelDef(
    name="sentiment",
    display_name="Sentiment Model",
    model_type="AGENT",
    type_color="#CC7700",
    description="FinBERT + RSS news headlines sentiment analysis.",
    weight_key="model_weights.sentiment",
    default_weight=0.12,
    enabled_by_default=True,
    params=[
        _p("models.sentiment.min_signal", "Min Signal Strength", "float", 0.35,
           min_val=0.10, max_val=0.80, step=0.05,
           desc="Minimum |signal_val| (0-1) to fire.",
           section="Entry"),
        _p("models.sentiment.min_confidence", "Min Confidence", "float", 0.55,
           min_val=0.30, max_val=0.90, step=0.05,
           desc="Minimum confidence score (0-1).",
           section="Entry"),
        _p("models.sentiment.min_headlines", "Min Headlines", "int", 3,
           min_val=1, max_val=10, step=1,
           desc="Minimum matching headlines to trigger.",
           section="Entry"),
        _p("models.sentiment.max_age_minutes", "Max Headline Age", "int", 90,
           min_val=60, max_val=1440, step=60,
           desc="Maximum headline age in minutes (1.5h default).",
           section="Entry"),
        _p("models.sentiment.sl_atr_mult", "Stop-Loss ATR Mult", "float", 1.5,
           min_val=0.5, max_val=3.0, step=0.1,
           desc="ATR multiplier for stop-loss.",
           section="Exit"),
        _p("models.sentiment.tp_atr_mult", "Take-Profit ATR Mult", "float", 2.5,
           min_val=1.0, max_val=5.0, step=0.1,
           desc="ATR multiplier for take-profit.",
           section="Exit"),
    ]
)


# ML MODELS (Purple #AA44CC)
# ────────────────────────────────────────────────────────

RL_ENSEMBLE = ModelDef(
    name="rl_ensemble",
    display_name="RL Ensemble",
    model_type="ML",
    type_color="#AA44CC",
    description="Reinforcement learning agents (SAC, CPPO, Duelling DQN).",
    weight_key="model_weights.rl_ensemble",
    default_weight=0.0,  # DISABLED by default until trained
    enabled_by_default=False,
    params=[
        _p("rl.enabled", "Enable RL Training", "bool", False,
           desc="Enable RL trainer to learn from trade outcomes.",
           section="Training"),
        _p("rl.replay_buffer_size", "Replay Buffer Size", "int", 50000,
           min_val=10000, max_val=200000, step=10000,
           desc="Maximum transitions to store in replay memory.",
           section="Training"),
        _p("rl.train_every_n_candles", "Train Every N Candles", "int", 10,
           min_val=1, max_val=50, step=1,
           desc="Frequency of RL model updates (candles).",
           section="Training"),
        _p("rl.reward_leverage", "Reward Leverage", "float", 10.0,
           min_val=1.0, max_val=50.0, step=1.0,
           desc="Scaling factor for RL reward signal.",
           section="Training"),
    ]
)


# META MODELS (Teal #008888)
# ────────────────────────────────────────────────────────

ORCHESTRATOR = ModelDef(
    name="orchestrator",
    display_name="Orchestrator",
    model_type="META",
    type_color="#008888",
    description="Meta-decision engine: conflict resolution & macro veto.",
    weight_key="model_weights.orchestrator",
    default_weight=0.22,
    enabled_by_default=True,
    params=[
        _p("orchestrator.veto_enabled", "Enable Veto", "bool", True,
           desc="Allow orchestrator to veto conflicting signals.",
           section="Orchestration"),
    ]
)


# ============================================================
# Global / Cross-Cutting Parameters
# ============================================================

GLOBAL_PARAMS = [
    # Scoring parameters
    _p("idss.min_confluence_score", "Min Confluence Score", "float", 0.45,
       min_val=0.20, max_val=0.90, step=0.05,
       desc="Minimum confluence score to generate a candidate.",
       section="Scoring"),
    _p("confluence.min_direction_dominance", "Min Direction Dominance", "float", 0.30,
       min_val=0.10, max_val=0.60, step=0.05,
       desc="Direction strength threshold for tie-breaking.",
       section="Scoring"),

    # Expected Value gate parameters
    _p("expected_value.ev_threshold", "EV Threshold", "float", 0.05,
       min_val=0.0, max_val=0.30, step=0.01,
       desc="Minimum expected value in R-multiples.",
       section="EV Gate"),
    _p("expected_value.min_rr_floor", "Min R:R Floor", "float", 1.0,
       min_val=0.5, max_val=3.0, step=0.1,
       desc="Minimum risk:reward ratio sanity check.",
       section="EV Gate"),
    _p("expected_value.sigmoid_steepness", "Sigmoid Steepness", "float", 8.0,
       min_val=2.0, max_val=20.0, step=0.5,
       desc="Sharpness of win_prob sigmoid curve.",
       section="EV Gate"),
    _p("expected_value.score_midpoint", "Score Midpoint", "float", 0.50,
       min_val=0.30, max_val=0.70, step=0.02,
       desc="Confluence score at 50% predicted win rate.",
       section="EV Gate"),
    _p("expected_value.regime_uncertainty_penalty", "Uncertainty Penalty", "float", 0.15,
       min_val=0.0, max_val=0.40, step=0.01,
       desc="Win probability penalty in uncertain regime.",
       section="EV Gate"),

    # Position sizing parameters
    _p("risk_engine.kelly_fraction", "Kelly Fraction", "float", 0.25,
       min_val=0.05, max_val=0.50, step=0.05,
       desc="Fraction of full Kelly criterion (0.25 = quarter-Kelly).",
       section="Position Sizing"),
    _p("risk_engine.max_position_pct", "Max Position %", "float", 0.04,
       min_val=0.01, max_val=0.10, step=0.01,
       desc="Maximum per-trade size as % of capital.",
       section="Position Sizing"),
    _p("risk_engine.min_position_pct", "Min Position %", "float", 0.003,
       min_val=0.001, max_val=0.01, step=0.001,
       desc="Minimum per-trade size as % of capital.",
       section="Position Sizing"),
    _p("risk_engine.portfolio_heat_max_pct", "Portfolio Heat Max %", "float", 0.06,
       min_val=0.02, max_val=0.15, step=0.01,
       desc="Maximum total portfolio risk (sum of all open trades).",
       section="Position Sizing"),
    _p("risk_engine.max_positions_per_symbol", "Max Positions Per Symbol", "int", 10,
       min_val=1, max_val=20, step=1,
       desc="Maximum concurrent long/short positions per pair.",
       section="Position Sizing"),

    # Risk management parameters
    _p("risk_engine.loss_streak_trigger", "Loss Streak Trigger", "int", 3,
       min_val=2, max_val=10, step=1,
       desc="Consecutive losses before size reduction.",
       section="Risk"),
    _p("risk_engine.loss_streak_size_multiplier", "Loss Streak Multiplier", "float", 0.50,
       min_val=0.10, max_val=1.00, step=0.05,
       desc="Position size multiplier during loss streak.",
       section="Risk"),

    # Multi-timeframe parameters
    _p("multi_tf.confirmation_required", "Require MTF Confirmation", "bool", True,
       desc="Require 4h regime confirmation for 1h signals.",
       section="MTF"),

    # Activation parameters
    _p("adaptive_activation.min_activation_weight", "Min Activation Weight", "float", 0.10,
       min_val=0.0, max_val=0.50, step=0.05,
       desc="Minimum model activation weight in uncertain regimes.",
       section="Activation"),

    # LTF Confirmation parameters
    _p("ltf_confirmation.rsi_max_long", "LTF RSI Max Long", "float", 72.0,
       min_val=55.0, max_val=90.0, step=1.0,
       desc="Reject long if 15m RSI above this value.",
       section="LTF Confirmation"),
    _p("ltf_confirmation.rsi_min_short", "LTF RSI Min Short", "float", 28.0,
       min_val=10.0, max_val=45.0, step=1.0,
       desc="Reject short if 15m RSI below this value.",
       section="LTF Confirmation"),
    _p("ltf_confirmation.rsi_void_long", "LTF RSI Void Long", "float", 78.0,
       min_val=60.0, max_val=95.0, step=1.0,
       desc="Void long candidate entirely if 15m RSI above this.",
       section="LTF Confirmation"),
    _p("ltf_confirmation.rsi_void_short", "LTF RSI Void Short", "float", 22.0,
       min_val=5.0, max_val=40.0, step=1.0,
       desc="Void short candidate entirely if 15m RSI below this.",
       section="LTF Confirmation"),
    _p("ltf_confirmation.volume_ratio_min", "LTF Min Volume Ratio", "float", 0.6,
       min_val=0.3, max_val=2.0, step=0.1,
       desc="Minimum 15m volume vs 20-bar average to confirm.",
       section="LTF Confirmation"),
    _p("ltf_confirmation.ema_period", "LTF EMA Period", "int", 9,
       min_val=3, max_val=30, step=1,
       desc="EMA span for 15m trend alignment check.",
       section="LTF Confirmation"),
]


# ============================================================
# Complete Strategy Registry
# ============================================================

STRATEGY_REGISTRY = [
    # CORE models (sorted by default weight descending)
    TREND_MODEL,
    VWAP_REVERSION_MODEL,
    MOMENTUM_BREAKOUT_MODEL,
    MEAN_REVERSION_MODEL,
    LIQUIDITY_SWEEP_MODEL,

    # AGENT models
    FUNDING_RATE_MODEL,
    ORDER_BOOK_MODEL,
    SENTIMENT_MODEL,

    # ML models
    RL_ENSEMBLE,

    # META models
    ORCHESTRATOR,
]


# ============================================================
# Registry Query Functions
# ============================================================

def get_model_def(name: str) -> Optional[ModelDef]:
    """
    Look up a model definition by name.

    Parameters
    ----------
    name : str
        Internal model name (e.g. "trend", "momentum_breakout")

    Returns
    -------
    ModelDef or None if not found
    """
    for model_def in STRATEGY_REGISTRY:
        if model_def.name == name:
            return model_def
    return None


def get_all_config_keys() -> list[str]:
    """
    Return all dotted config keys across all models and global parameters.

    Returns
    -------
    list[str]
        Sorted list of unique config keys (e.g. ["models.trend.adx_min", ...])
    """
    keys = set()
    for model_def in STRATEGY_REGISTRY:
        for param in model_def.params:
            keys.add(param.key)
        keys.add(model_def.weight_key)
    for param in GLOBAL_PARAMS:
        keys.add(param.key)
    return sorted(list(keys))


def is_model_enabled(name: str) -> bool:
    """
    Check if a model is enabled (not in disabled_models list).

    Parameters
    ----------
    name : str
        Internal model name

    Returns
    -------
    bool
        True if enabled, False if disabled
    """
    try:
        from config.settings import settings
        disabled = settings.get("disabled_models", [])
        return name not in disabled
    except Exception as e:
        logger.warning(f"Could not check model enabled status: {e}")
        model_def = get_model_def(name)
        return model_def.enabled_by_default if model_def else True


def get_model_weight(name: str) -> float:
    """
    Get the current weight for a model (from config or default).

    Parameters
    ----------
    name : str
        Internal model name

    Returns
    -------
    float
        Current weight (0.0-1.0)
    """
    model_def = get_model_def(name)
    if not model_def:
        return 0.0

    try:
        from config.settings import settings
        return settings.get(model_def.weight_key, model_def.default_weight)
    except Exception as e:
        logger.warning(f"Could not load weight for {name}: {e}")
        return model_def.default_weight


def get_param_def(key: str) -> Optional[ModelParamDef]:
    """
    Look up a parameter definition by dotted config key.

    Parameters
    ----------
    key : str
        Dotted config key (e.g. "models.trend.adx_min")

    Returns
    -------
    ModelParamDef or None if not found
    """
    # Search model parameters
    for model_def in STRATEGY_REGISTRY:
        for param in model_def.params:
            if param.key == key:
                return param
    # Search global parameters
    for param in GLOBAL_PARAMS:
        if param.key == key:
            return param
    return None


def get_models_by_type(model_type: str) -> list[ModelDef]:
    """
    Get all models of a specific type.

    Parameters
    ----------
    model_type : str
        Type filter: "CORE", "AGENT", "ML", "META"

    Returns
    -------
    list[ModelDef]
        Models matching the type
    """
    return [m for m in STRATEGY_REGISTRY if m.model_type == model_type]


def validate_registry() -> tuple[bool, list[str]]:
    """
    Validate the entire strategy registry for consistency.

    Returns
    -------
    tuple[bool, list[str]]
        (is_valid, list of error messages)
    """
    errors = []
    seen_names = set()

    for model_def in STRATEGY_REGISTRY:
        # Check unique names
        if model_def.name in seen_names:
            errors.append(f"Duplicate model name: {model_def.name}")
        seen_names.add(model_def.name)

        # Check unique parameter keys
        seen_keys = set()
        for param in model_def.params:
            if param.key in seen_keys:
                errors.append(f"Duplicate param key in {model_def.name}: {param.key}")
            seen_keys.add(param.key)

    # Check global param keys don't duplicate model params
    model_keys = set()
    for model_def in STRATEGY_REGISTRY:
        for param in model_def.params:
            model_keys.add(param.key)

    for param in GLOBAL_PARAMS:
        if param.key in model_keys:
            errors.append(f"Global param key conflicts with model param: {param.key}")

    is_valid = len(errors) == 0
    return is_valid, errors


# ============================================================
# Module Initialization
# ============================================================

if __name__ == "__main__":
    # Self-test on import
    is_valid, errors = validate_registry()
    if not is_valid:
        logger.error(f"Strategy registry validation failed: {errors}")
    else:
        logger.info(f"Strategy registry loaded: {len(STRATEGY_REGISTRY)} models, "
                   f"{len(GLOBAL_PARAMS)} global params, "
                   f"{len(get_all_config_keys())} total config keys")
