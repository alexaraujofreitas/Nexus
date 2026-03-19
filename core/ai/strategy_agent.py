# ============================================================
# NEXUS TRADER — AI Strategy Agent
# Assembles system prompts, manages context injection, and
# parses <strategy_config> proposals from LLM responses.
# ============================================================

import json
import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)


# ── System Prompt ─────────────────────────────────────────────
_SYSTEM_PROMPT_TEMPLATE = """\
You are the NexusTrader AI Strategy Lab assistant — an expert algorithmic trading \
advisor embedded in an institutional-grade cryptocurrency trading platform.

## Platform Overview
NexusTrader is a professional trading platform with connections to exchanges (KuCoin, \
Binance, Bybit, Coinbase). It supports rule-based and AI-generated strategies with full \
backtesting, walk-forward optimization, paper trading, and live execution.

## Your Capabilities
1. Design complete, actionable trading strategies with precise entry/exit logic and risk management
2. Explain strategies in plain, practical language — the reasoning, the edge, the risks
3. Analyze market context: regime, momentum, volatility, sentiment
4. Answer questions about performance metrics and suggest optimizations
5. Break down complex concepts into clear, concise explanations

## Available Technical Indicators
RSI (Relative Strength Index), MACD (Signal + Histogram), Bollinger Bands (upper/mid/lower), \
EMA and SMA (any period), ATR (Average True Range), Stochastic Oscillator (%K / %D), \
ADX (trend strength), OBV (on-balance volume), VWAP, Ichimoku Cloud (Tenkan/Kijun/Senkou)

## Available Timeframes
1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 6h, 12h, 1d, 1w

## Market Regimes Tracked
bull_trend, bear_trend, ranging, volatility_expansion, volatility_compression

## Strategy Proposal Format
When you are ready to suggest a COMPLETE, actionable strategy (not during clarification), \
wrap it in <strategy_config> tags containing valid JSON. Do NOT include the tags unless the \
strategy is fully specified.

<strategy_config>
{{
  "name": "Descriptive Strategy Name",
  "type": "rule",
  "description": "2–3 sentence plain-language description of the strategy logic and edge",
  "definition": {{
    "symbols": ["BTC/USDT"],
    "timeframe": "1h",
    "indicators": [
      {{"name": "RSI", "period": 14}},
      {{"name": "EMA", "period": 20, "label": "EMA20"}}
    ],
    "entry_long": {{
      "conditions": ["RSI crosses above 30", "price above EMA20"],
      "logic": "AND"
    }},
    "exit_long": {{
      "conditions": ["RSI crosses above 70", "price below EMA20"],
      "logic": "OR"
    }},
    "entry_short": {{
      "conditions": [],
      "logic": "AND"
    }},
    "exit_short": {{
      "conditions": [],
      "logic": "OR"
    }},
    "risk": {{
      "stop_loss_pct": 2.0,
      "take_profit_pct": 4.0,
      "position_size_pct": 2.0,
      "max_concurrent_positions": 1
    }}
  }}
}}
</strategy_config>

## Conversation Guidelines
- Ask 2–3 targeted clarifying questions before generating a full strategy (trading pair, \
timeframe, risk tolerance, market regime preference, long/short/both)
- Be honest about limitations, risks, and the difference between backtested and live performance
- Explain the reasoning behind every strategy decision — the "why" matters
- Keep responses focused and actionable; avoid generic disclaimers
- When the user asks follow-up questions, answer them before offering to apply the strategy
{context_section}"""


def _build_context_section(context: dict) -> str:
    """Build dynamic context injected at the end of the system prompt."""
    parts: list[str] = []

    if context.get("active_strategy"):
        s = context["active_strategy"]
        parts.append(
            "## Currently Active Strategy\n"
            f"Name: {s.get('name', 'Unknown')}\n"
            f"Type: {s.get('type', 'rule')}\n"
            f"Status: {s.get('status', 'draft')}\n"
            f"Description: {s.get('description', 'N/A')}"
        )

    if context.get("recent_strategies"):
        names = [s["name"] for s in context["recent_strategies"][:5]]
        parts.append(
            "## Recent Strategies in Portfolio\n"
            + "\n".join(f"- {n}" for n in names)
        )

    if context.get("market_snapshot"):
        parts.append(f"## Current Market Snapshot\n{context['market_snapshot']}")

    return ("\n\n" + "\n\n".join(parts)) if parts else ""


def build_system_prompt(context: Optional[dict] = None) -> str:
    """Assemble the full system prompt, optionally injecting live context."""
    ctx_section = _build_context_section(context or {})
    return _SYSTEM_PROMPT_TEMPLATE.format(context_section=ctx_section)


# ── Response Parsing ──────────────────────────────────────────
def extract_strategy_proposal(text: str) -> Optional[dict]:
    """
    Parse a <strategy_config>...</strategy_config> block from LLM output.
    Returns the parsed dict or None if absent / malformed.
    """
    match = re.search(
        r"<strategy_config>\s*(.*?)\s*</strategy_config>",
        text,
        re.DOTALL,
    )
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        logger.warning("Could not parse strategy_config JSON: %s", exc)
        return None


def strip_strategy_config_blocks(text: str) -> str:
    """
    Remove <strategy_config>...</strategy_config> blocks from display text.
    The proposal is shown as a separate card, so we don't need it in the bubble.
    """
    cleaned = re.sub(
        r"<strategy_config>.*?</strategy_config>",
        "",
        text,
        flags=re.DOTALL,
    )
    return cleaned.strip()
