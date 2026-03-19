# ============================================================
# NEXUS TRADER — AI Strategy Lab: Strategy Parser
# Parses LLM-generated strategy descriptions into executable
# IDSS sub-model configurations and StrategyRunner condition trees.
# ============================================================
from __future__ import annotations
import json
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

STRATEGY_SYSTEM_PROMPT = """You are an expert algorithmic trading strategy designer for NexusTrader.

When asked to design a strategy, respond ONLY with a JSON object using this schema:

{
  "name": "Strategy Name",
  "description": "Brief description",
  "type": "trend_following" | "mean_reversion" | "momentum_breakout" | "scalping" | "hybrid",
  "timeframe": "1m"|"5m"|"15m"|"1h"|"4h"|"1d",
  "regime_filter": ["bull_trend","bear_trend","ranging","volatility_expansion","any"],
  "entry_long": {
    "type": "AND",
    "children": [
      {"type":"condition","operator":">","left":{"type":"indicator","name":"rsi_14"},"right":{"type":"numeric","value":50}}
    ]
  },
  "entry_short": null,
  "stop_loss_pct": 2.0,
  "take_profit_pct": 4.0,
  "idss_model_weights": {"trend":0.4,"mean_reversion":0.2,"momentum_breakout":0.3,"sentiment":0.1},
  "min_confluence_score": 0.60,
  "regime_weights": {"TRENDING_UP":1.2,"RANGING":0.5,"CRISIS":0.0},
  "rationale": "Why this strategy works"
}

Available indicator names: rsi_14, rsi_7, rsi_21, ema_9, ema_20, ema_50, ema_200, sma_20, sma_50, sma_200, bb_upper, bb_lower, bb_middle, bb_width, macd_line, macd_signal, macd_histogram, adx, atr_14, volume, vwap, close, open, high, low

Operators: > < >= <= == crosses_above crosses_below pct_up pct_down

Output ONLY the JSON object, no markdown or explanation."""


class StrategyParser:
    """Parses LLM-generated strategy text into executable NexusTrader configs."""

    def parse(self, llm_response: str) -> Optional[dict]:
        parsed = self._extract_json(llm_response)
        if not parsed:
            logger.warning("StrategyParser: could not extract JSON from LLM response")
            return None
        if not self._validate(parsed):
            return None
        return self._normalize(parsed)

    def _extract_json(self, text: str) -> Optional[dict]:
        try:
            return json.loads(text.strip())
        except Exception:
            pass
        for pattern in [r'```json\s*([\s\S]*?)\s*```', r'```\s*([\s\S]*?)\s*```', r'\{[\s\S]*\}']:
            m = re.search(pattern, text, re.DOTALL)
            if m:
                candidate = m.group(1) if '```' in pattern else m.group(0)
                try:
                    return json.loads(candidate.strip())
                except Exception:
                    continue
        return None

    def _validate(self, config: dict) -> bool:
        for field in ["name", "type", "timeframe"]:
            if field not in config:
                logger.warning("StrategyParser: missing required field '%s'", field)
                return False
        if not config.get("entry_long") and not config.get("entry_short"):
            logger.warning("StrategyParser: no entry conditions")
            return False
        return True

    def _normalize(self, config: dict) -> dict:
        config.setdefault("stop_loss_pct", 2.0)
        config.setdefault("take_profit_pct", 4.0)
        config.setdefault("min_confluence_score", 0.55)
        config.setdefault("regime_filter", ["any"])
        config.setdefault("description", "")
        config.setdefault("rationale", "")
        config.setdefault("idss_model_weights", {})
        config.setdefault("regime_weights", {})
        config["stop_loss_pct"]        = max(0.5,  min(10.0, float(config["stop_loss_pct"])))
        config["take_profit_pct"]      = max(1.0,  min(20.0, float(config["take_profit_pct"])))
        config["min_confluence_score"] = max(0.40, min(0.95, float(config["min_confluence_score"])))
        return config

    def to_strategy_runner_config(self, parsed: dict) -> dict:
        return {
            "name":            parsed["name"],
            "entry_long":      parsed.get("entry_long"),
            "entry_short":     parsed.get("entry_short"),
            "stop_loss_pct":   parsed["stop_loss_pct"],
            "take_profit_pct": parsed["take_profit_pct"],
            "timeframe":       parsed["timeframe"],
        }

    def to_confluence_weight_overrides(self, parsed: dict) -> dict:
        return parsed.get("idss_model_weights", {})

    def get_system_prompt(self) -> str:
        return STRATEGY_SYSTEM_PROMPT


_parser: Optional[StrategyParser] = None


def get_strategy_parser() -> StrategyParser:
    global _parser
    if _parser is None:
        _parser = StrategyParser()
    return _parser
