# ============================================================
# NEXUS TRADER — StrategyRunner
# Translates Rule Builder condition trees into a CustomRuleModel
# that plugs into the live SignalGenerator pipeline.
# ============================================================
from __future__ import annotations
import logging
from typing import Optional
import pandas as pd
from core.signals.sub_models.custom_rule_model import CustomRuleModel

logger = logging.getLogger(__name__)


class StrategyRunner:
    """
    Bridges the Rule Builder UI and the live IDSS pipeline.
    Accepts a strategy definition and creates a CustomRuleModel
    that can be registered with the SignalGenerator.
    """

    def __init__(self):
        self._model: Optional[CustomRuleModel] = None
        self._strategy_name: str = ""
        self._active: bool = False

    def load_strategy(self, strategy: dict) -> bool:
        try:
            name       = strategy.get("name", "CustomStrategy")
            long_tree  = strategy.get("entry_long")
            short_tree = strategy.get("entry_short")
            sl_pct     = float(strategy.get("stop_loss_pct", 2.0))
            tp_pct     = float(strategy.get("take_profit_pct", 4.0))
            tf         = strategy.get("timeframe", "1h")

            if not long_tree and not short_tree:
                logger.warning("StrategyRunner: no entry conditions in strategy '%s'", name)
                return False

            self._model = CustomRuleModel(
                entry_long_tree  = long_tree,
                entry_short_tree = short_tree,
                stop_loss_pct    = sl_pct,
                take_profit_pct  = tp_pct,
                timeframe        = tf,
                rule_name        = name,
            )
            self._strategy_name = name
            logger.info("StrategyRunner: loaded strategy '%s'", name)
            return True
        except Exception as exc:
            logger.error("StrategyRunner: load_strategy failed: %s", exc)
            return False

    def activate(self) -> bool:
        if self._model is None:
            logger.warning("StrategyRunner: call load_strategy() first")
            return False
        try:
            from core.signals.signal_generator import get_signal_generator
            get_signal_generator().register_custom_model(self._model)
            self._active = True
            logger.info("StrategyRunner: '%s' ACTIVATED in live pipeline", self._strategy_name)
            return True
        except Exception as exc:
            logger.error("StrategyRunner: activate failed: %s", exc)
            return False

    def deactivate(self) -> bool:
        try:
            from core.signals.signal_generator import get_signal_generator
            get_signal_generator().unregister_custom_model()
            self._active = False
            logger.info("StrategyRunner: '%s' DEACTIVATED", self._strategy_name)
            return True
        except Exception as exc:
            logger.error("StrategyRunner: deactivate failed: %s", exc)
            return False

    def test_on_data(self, df: pd.DataFrame, symbol: str = "BTC/USDT", timeframe: str = "1h") -> list[dict]:
        if self._model is None or df is None or df.empty:
            return []
        results = []
        for i in range(1, len(df)):
            window = df.iloc[:i+1]
            sigs = self._model.generate(symbol, window, timeframe=timeframe)
            for sig in sigs:
                results.append({
                    "bar_index": i,
                    "timestamp": str(df.index[i]),
                    "direction": sig.direction,
                    "strength":  round(sig.strength, 3),
                })
        return results

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def strategy_name(self) -> str:
        return self._strategy_name

    @property
    def model(self) -> Optional[CustomRuleModel]:
        return self._model


_runner: Optional[StrategyRunner] = None


def get_strategy_runner() -> StrategyRunner:
    global _runner
    if _runner is None:
        _runner = StrategyRunner()
    return _runner
