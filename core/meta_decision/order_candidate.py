# ============================================================
# NEXUS TRADER — Order Candidate & Model Signal
#
# Core data structures flowing through the IDSS pipeline:
#   ModelSignal  — output of one sub-model for one asset
#   OrderCandidate — confluence-scored, risk-checked trade proposal
# ============================================================
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class ModelSignal:
    """
    Output of a single sub-model for one asset/timeframe.
    Produced by TrendModel, MeanReversionModel, MomentumBreakoutModel,
    or LiquiditySweepModel and consumed by ConfluenceScorer.
    """
    symbol:       str          # e.g. "BTC/USDT"
    model_name:   str          # "trend" | "mean_reversion" | "momentum_breakout" | "liquidity_sweep"
    direction:    str          # "long" | "short"
    strength:     float        # 0.0–1.0  (model's own confidence)
    entry_price:  float        # suggested entry price
    stop_loss:    float        # absolute stop-loss price
    take_profit:  float        # absolute take-profit price
    timeframe:    str          # primary timeframe this signal was generated on
    regime:       str          # regime detected when signal fired
    rationale:    str          # plain-text explanation from this model
    atr_value:    float        # ATR14 at generation time (used for stop sizing)
    timestamp:    datetime     = field(default_factory=datetime.utcnow)


@dataclass
class OrderCandidate:
    """
    A confluence-scored trade proposal ready to enter the risk gate.
    Created by ConfluenceScorer from one or more ModelSignals.
    Not yet an Order — must pass RiskGate before execution.
    """
    symbol:             str
    side:               str              # "buy" | "sell"
    entry_type:         str              # "market" | "limit" | "conditional_limit"
    entry_price:        Optional[float]  # None → market order
    stop_loss_price:    float
    take_profit_price:  float
    position_size_usdt: float            # preliminary size suggestion
    score:              float            # 0.0–1.0 confluence score
    models_fired:       list[str]        # sub-model names that contributed
    regime:             str              # regime at generation time
    rationale:          str              # combined plain-text explanation
    timeframe:          str
    atr_value:          float
    generated_at:       datetime         = field(default_factory=datetime.utcnow)
    expiry:             Optional[datetime] = None   # discard if not filled by this time
    risk_reward_ratio:  float            = 0.0     # (take_profit - entry) / (entry - stop_loss)
    approved:           bool             = False    # set True by RiskGate
    rejection_reason:   Optional[str]   = None
    requires_confirmation: bool          = False    # True if awaiting UI confirmation in live mode
    candidate_id:       str              = ""       # UUID for tracking in pending confirmations
    higher_tf_regime:   str              = ""       # regime on the next-higher timeframe (for MTF check)
    expected_value:     float            = 0.0      # EV score = win_prob * reward - loss_prob * risk

    def __post_init__(self):
        if self.entry_price and self.entry_price > 0:
            risk   = abs(self.entry_price - self.stop_loss_price)
            reward = abs(self.take_profit_price - self.entry_price)
            self.risk_reward_ratio = round(reward / risk, 2) if risk > 0 else 0.0

    def to_dict(self) -> dict:
        return {
            "symbol":             self.symbol,
            "side":               self.side,
            "entry_type":         self.entry_type,
            "entry_price":        self.entry_price,
            "stop_loss_price":    self.stop_loss_price,
            "take_profit_price":  self.take_profit_price,
            "position_size_usdt": self.position_size_usdt,
            "score":              round(self.score, 4),
            "models_fired":       self.models_fired,
            "regime":             self.regime,
            "rationale":          self.rationale,
            "timeframe":          self.timeframe,
            "atr_value":          self.atr_value,
            "risk_reward_ratio":  self.risk_reward_ratio,
            "generated_at":       self.generated_at.isoformat(),
            "approved":           self.approved,
            "rejection_reason":   self.rejection_reason,
            "requires_confirmation": self.requires_confirmation,
            "candidate_id":       self.candidate_id,
        }
