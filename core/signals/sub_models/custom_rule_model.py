# ============================================================
# NEXUS TRADER — CustomRuleModel Sub-Model
# Translates Rule Builder condition trees into IDSS signals.
# Allows non-programmer users to define entry logic in the UI.
# ============================================================
from __future__ import annotations
import logging
from typing import Optional
import pandas as pd
from core.meta_decision.order_candidate import ModelSignal

logger = logging.getLogger(__name__)


class CustomRuleModel:
    """
    A SignalGenerator sub-model that evaluates a user-defined condition tree
    built in the Rule Builder UI.
    """

    model_name = "custom_rule"

    def __init__(
        self,
        entry_long_tree:  Optional[dict] = None,
        entry_short_tree: Optional[dict] = None,
        stop_loss_pct:    float = 2.0,
        take_profit_pct:  float = 4.0,
        timeframe:        str = "1h",
        rule_name:        str = "CustomRule",
    ):
        self._long_tree  = entry_long_tree
        self._short_tree = entry_short_tree
        self._sl_pct     = stop_loss_pct / 100.0
        self._tp_pct     = take_profit_pct / 100.0
        self._timeframe  = timeframe
        self._rule_name  = rule_name

    def generate(
        self,
        symbol:    str,
        df:        pd.DataFrame,
        regime:    str = "",
        timeframe: str = "",
    ) -> list[ModelSignal]:
        if df is None or len(df) < 2:
            return []
        if not self._long_tree and not self._short_tree:
            return []

        signals = []
        close = float(df["close"].iloc[-1])
        tf = timeframe or self._timeframe

        try:
            if self._long_tree and self._eval_tree(self._long_tree, df):
                sl = close * (1.0 - self._sl_pct)
                tp = close * (1.0 + self._tp_pct)
                signals.append(ModelSignal(
                    model_name  = self.model_name,
                    direction   = "long",
                    strength    = 0.65,
                    entry_price = close,
                    stop_loss   = sl,
                    take_profit = tp,
                    timeframe   = tf,
                    regime      = regime,
                    rationale   = f"{self._rule_name}: long entry conditions met",
                ))
        except Exception as exc:
            logger.debug("CustomRuleModel long eval error: %s", exc)

        try:
            if self._short_tree and self._eval_tree(self._short_tree, df):
                sl = close * (1.0 + self._sl_pct)
                tp = close * (1.0 - self._tp_pct)
                signals.append(ModelSignal(
                    model_name  = self.model_name,
                    direction   = "short",
                    strength    = 0.65,
                    entry_price = close,
                    stop_loss   = sl,
                    take_profit = tp,
                    timeframe   = tf,
                    regime      = regime,
                    rationale   = f"{self._rule_name}: short entry conditions met",
                ))
        except Exception as exc:
            logger.debug("CustomRuleModel short eval error: %s", exc)

        return signals

    def _eval_tree(self, node: dict, df: pd.DataFrame) -> bool:
        node_type = node.get("type", "condition")
        if node_type == "AND":
            return all(self._eval_tree(child, df) for child in node.get("children", []))
        elif node_type == "OR":
            return any(self._eval_tree(child, df) for child in node.get("children", []))
        elif node_type == "condition":
            return self._eval_condition(node, df)
        return False

    def _eval_condition(self, node: dict, df: pd.DataFrame) -> bool:
        operator  = node.get("operator", ">")
        left_val  = self._resolve_value(node.get("left", {}),  df)
        right_val = self._resolve_value(node.get("right", {}), df)
        if left_val is None or right_val is None:
            return False
        if operator == ">":   return left_val > right_val
        if operator == "<":   return left_val < right_val
        if operator == ">=":  return left_val >= right_val
        if operator == "<=":  return left_val <= right_val
        if operator == "==":  return abs(left_val - right_val) < 1e-9
        if operator in ("crosses_above", "crosses_below"):
            left_prev  = self._resolve_value(node.get("left", {}),  df, offset=1)
            right_prev = self._resolve_value(node.get("right", {}), df, offset=1)
            if left_prev is None or right_prev is None:
                return False
            if operator == "crosses_above":
                return (left_prev <= right_prev) and (left_val > right_val)
            else:
                return (left_prev >= right_prev) and (left_val < right_val)
        if operator == "pct_up":
            n = int(node.get("lookback", 5))
            if len(df) < n + 1:
                return False
            past = self._resolve_value(node.get("left", {}), df, offset=n)
            if past and past > 0:
                return (left_val - past) / past * 100.0 >= right_val
        if operator == "pct_down":
            n = int(node.get("lookback", 5))
            if len(df) < n + 1:
                return False
            past = self._resolve_value(node.get("left", {}), df, offset=n)
            if past and past > 0:
                return (past - left_val) / past * 100.0 >= right_val
        return False

    def _resolve_value(self, spec, df: pd.DataFrame, offset: int = 0) -> Optional[float]:
        if isinstance(spec, (int, float)):
            return float(spec)
        if isinstance(spec, dict):
            spec_type = spec.get("type", "indicator")
            if spec_type == "numeric":
                return float(spec.get("value", 0.0))
            if spec_type == "indicator":
                col = spec.get("name", "")
                idx = -(1 + offset)
                if col in df.columns and len(df) >= abs(idx):
                    val = df[col].iloc[idx]
                    if pd.notna(val):
                        return float(val)
        return None

    def update_trees(self, entry_long_tree=None, entry_short_tree=None) -> None:
        self._long_tree  = entry_long_tree
        self._short_tree = entry_short_tree
        logger.info("CustomRuleModel '%s': condition trees updated", self._rule_name)
