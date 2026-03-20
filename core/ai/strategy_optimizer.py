# ============================================================
# NEXUS TRADER — AI Strategy Optimizer
#
# Analyzes backtest results (metrics + losing trade sample)
# and produces an improved strategy via the configured LLM.
#
# Outputs two tagged blocks:
#   <strategy_config>  … complete JSON definition …  </strategy_config>
#   <changes>          … numbered change-log …        </changes>
# ============================================================

import json
import re
import logging
from collections import Counter
from typing import Optional

logger = logging.getLogger(__name__)


# ── System Prompt ─────────────────────────────────────────────
OPTIMIZER_SYSTEM_PROMPT = """\
You are an expert quantitative trading analyst and strategy optimizer embedded in \
NexusTrader, an institutional-grade cryptocurrency trading platform.

## Your Role
You receive a complete trading strategy definition plus its backtest results. \
You analyze the weaknesses systematically and produce a concretely improved strategy \
that addresses those exact weaknesses — keeping the core logic intact.

## Available Technical Indicators
RSI, MACD (Signal + Histogram), Bollinger Bands (upper/mid/lower), EMA, SMA (any period), \
ATR (Average True Range), Stochastic (%K/%D), ADX (trend strength), OBV (on-balance volume), \
VWAP, Ichimoku Cloud (Tenkan/Kijun/Senkou)

## Available Timeframes
1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 6h, 12h, 1d, 1w

## Optimization Principles
1. Root-cause first — identify WHY trades fail before changing parameters
2. Targeted changes — make the minimum number of changes needed to fix weaknesses
3. Structural improvements only — do not curve-fit to match this exact historical period
4. Preserve the edge — keep the core signal that makes logical sense
5. Risk sizing first — fix stop/take-profit alignment with volatility before entry tuning

## Common Weakness Patterns and Fixes
- High stop-loss rate → stop too tight; widen it or add an ATR buffer
- Low win rate + MACD trigger → add ADX > 20 filter to trade only in trending regimes
- Many trades, low profit factor → RSI momentum cap too loose; tighten upper bound
- EMA filter too fast (12/55) → switch to longer periods (20/100) to reduce choppy-market signals
- Excessive consecutive losses → add a higher-timeframe trend bias check (e.g., price > 200 EMA on 1h)
- Stop-loss and take-profit too symmetrical → risk:reward of 1.8%/3.0% should be at least 1:1.5

## Required Output Format
You MUST output EXACTLY these two tagged blocks and nothing else after the analysis:

<strategy_config>
{ valid JSON — complete optimized strategy definition }
</strategy_config>

<changes>
Numbered list: each item states what changed, the old value → new value,
and the specific quantitative reason from the backtest data.
</changes>

## Strategy JSON Schema
{
  "name": "...",
  "type": "ai",
  "description": "2-3 sentence description of the optimized strategy logic and edge",
  "definition": {
    "symbols": ["BTC/USDT"],
    "timeframe": "1m",
    "timeframes": ["1m", "5m", "15m"],
    "indicators": [
      {"name": "EMA",  "period": 12, "timeframe": "15m", "label": "EMA12"},
      {"name": "RSI",  "period": 14, "timeframe": "5m",  "label": "RSI14_5m"},
      {"name": "MACD", "fast": 12, "slow": 26, "signal": 9, "timeframe": "5m", "label": "MACD"}
    ],
    "entry_long":  {"conditions": ["..."], "logic": "AND"},
    "exit_long":   {"conditions": ["stop_loss 1.8%", "take_profit 3.0% then trailing_stop 1.0%"], "logic": "OR"},
    "entry_short": {"conditions": [], "logic": "AND"},
    "exit_short":  {"conditions": [], "logic": "OR"},
    "risk": {
      "stop_loss_pct": 1.8,
      "take_profit_pct": 3.0,
      "trailing_stop_pct": 1.0,
      "trailing_stop_trigger_pct": 3.0,
      "position_size_usdt": 40.0,
      "max_concurrent_positions": 2,
      "order_type": "market"
    }
  }
}

## ⚠️  CONDITION FORMAT — STRICT RULES (read carefully, parser enforces this)
Every string in `"conditions"` arrays is parsed by an automated condition engine.
Only the formats listed below are valid.  Any other format will fail silently.

### VALID condition formats

  Indicator vs numeric value (most common):
    "RSI14 > 45"
    "RSI14 < 70"
    "ADX > 25"
    "ADX14 > 20"

  Indicator vs indicator:
    "EMA9 > EMA21"
    "EMA20 > EMA100"
    "close > EMA200"
    "price > EMA50"
    "MACD above Signal Line"

  Crossover events:
    "EMA9 crosses above EMA21"
    "EMA9 crosses below EMA21"
    "MACD crosses above signal line"
    "MACD crosses below signal line"
    "RSI14 crosses above 30"
    "RSI14 crosses below 70"

  With timeframe suffix (append _TF to both sides if needed):
    "EMA20_1h > EMA100_1h"
    "RSI14_1h < 65"
    "ADX14_1h > 25"

  With inline timeframe (append  on TF  to the LHS indicator):
    "MACD on 5m above Signal Line"
    "RSI14 on 15m > 45"
    "EMA20 on 1h > EMA100 on 1h"

  Valid indicator names (use exactly these, no parentheses):
    RSI, RSI14, RSI7, EMA9, EMA12, EMA20, EMA21, EMA50, EMA55, EMA100, EMA200,
    SMA20, SMA50, SMA200, MACD, Signal Line, ADX, ADX14, ATR, ATR14,
    BB upper, BB lower, BB mid, VWAP, close, price, volume, open, high, low

### ❌ FORBIDDEN formats (these will BREAK the parser — never use them)

  ❌  "RSI14 > 40 AND ADX > 25"        ← compound in one string (split into TWO conditions)
  ❌  "RSI14 > 40 OR MACD > 0"         ← compound OR in one string (use "logic": "OR" instead)
  ❌  "RSI between 40 and 70"           ← range syntax (write as TWO conditions: "RSI14 > 40" + "RSI14 < 70")
  ❌  "volume > 1.5 * SMA20"            ← arithmetic expressions
  ❌  "ADX(14) > 25"                    ← parentheses in indicator name (write "ADX14 > 25")
  ❌  "Price must be above the 200 EMA" ← prose descriptions
  ❌  "EMA(9) crosses above EMA(21)"    ← parentheses anywhere
  ❌  "trend is bullish"                ← abstract/non-numeric conditions

### Correct way to express compound conditions
  Instead of: "RSI14 > 40 AND ADX > 25"  (one string — WRONG)
  Write:       ["RSI14 > 40", "ADX > 25"]  in the conditions array with "logic": "AND"

  Instead of: "RSI between 40 and 70"  (range — WRONG)
  Write:       ["RSI14 > 40", "RSI14 < 70"]  in the conditions array

## Hard Rules
- Keep the same trading pair(s) and direction as the original
- Output valid JSON only — no comments, no trailing commas, no markdown code fences inside the tags
- The <changes> block must be plain numbered text — no JSON, no code blocks
- Reference actual numbers from the provided metrics in every change rationale
- Name the optimized strategy exactly as specified in the prompt (do not invent a new name)
- Every single condition string MUST follow the VALID formats above — no exceptions
"""


# ── Prompt builder ────────────────────────────────────────────
def build_optimizer_prompt(
    strategy:         dict,
    metrics:          dict,
    trades:           list,
    symbol:           str,
    timeframe:        str,
    new_name:         str,
    date_range:       str = "",
    max_trade_sample: int = 30,
) -> str:
    """Construct the user message sent to the optimizer LLM.

    ``max_trade_sample`` controls how many losing trades are included in the
    prompt.  Use a smaller value (e.g. 10) for local / smaller LLMs to keep
    the total prompt within the model's effective context window.
    """

    defn = strategy.get("definition") or {}

    # Partition trades
    losing  = [t for t in trades if t.get("pnl", 0) < 0]
    winning = [t for t in trades if t.get("pnl", 0) >= 0]

    # Exit reason table
    all_reasons    = Counter(t.get("exit_reason", "unknown") for t in trades)
    losing_reasons = Counter(t.get("exit_reason", "unknown") for t in losing)
    reason_lines = "\n".join(
        f"  {reason:20s}  total={count}  losing={losing_reasons.get(reason, 0)}"
        for reason, count in all_reasons.most_common()
    )

    # Max consecutive losses
    max_consec = cur_consec = 0
    for t in trades:
        if t.get("pnl", 0) < 0:
            cur_consec += 1
            max_consec  = max(max_consec, cur_consec)
        else:
            cur_consec = 0

    # Average loss and average win
    avg_loss = (sum(t.get("pnl_pct", 0) for t in losing)  / len(losing))  if losing  else 0.0
    avg_win  = (sum(t.get("pnl_pct", 0) for t in winning) / len(winning)) if winning else 0.0

    # Losing trade sample (capped by max_trade_sample)
    sample = losing[:max_trade_sample]
    sample_text = "\n".join(
        f"  {t.get('entry_time','')[:16]} → {t.get('exit_time','')[:16]}  "
        f"Entry={t.get('entry_price',0):.4g}  Exit={t.get('exit_price',0):.4g}  "
        f"PnL={t.get('pnl_pct',0):+.2f}%  Reason={t.get('exit_reason','')}"
        for t in sample
    ) or "  (no losing trades)"

    return f"""Optimize the trading strategy below based on its backtest results.

═══════════════════════════════════════════════════════════
STRATEGY TO OPTIMIZE
═══════════════════════════════════════════════════════════
Original name : {strategy.get('name', 'Unknown')}
New name      : {new_name}        ← use this exact name in the output
Symbol        : {symbol}
Timeframe     : {timeframe}
Date range    : {date_range or 'not specified'}

CURRENT DEFINITION (JSON):
{json.dumps(defn, indent=2)}

═══════════════════════════════════════════════════════════
BACKTEST PERFORMANCE SUMMARY
═══════════════════════════════════════════════════════════
Total Return         : {metrics.get('total_return_pct', 0):+.2f}%
Win Rate             : {metrics.get('win_rate', 0):.1f}%
Sharpe Ratio         : {metrics.get('sharpe_ratio', 0):.3f}
Max Drawdown         : -{metrics.get('max_drawdown_pct', 0):.2f}%
Profit Factor        : {metrics.get('profit_factor', 0):.3f}
Total Trades         : {metrics.get('total_trades', 0)}
Winning Trades       : {metrics.get('winning_trades', 0)}   (avg {avg_win:+.2f}%)
Losing Trades        : {metrics.get('losing_trades', 0)}   (avg {avg_loss:+.2f}%)
Max Consecutive Loss : {max_consec}

EXIT REASON BREAKDOWN:
{reason_lines}

═══════════════════════════════════════════════════════════
LOSING TRADE SAMPLE (first {len(sample)} of {len(losing)} losing trades)
═══════════════════════════════════════════════════════════
{sample_text}

═══════════════════════════════════════════════════════════
TASK
═══════════════════════════════════════════════════════════
1. Identify the 2-4 root causes of poor performance from the data above.
2. Propose specific, targeted fixes for each root cause.
3. Output the complete optimized strategy as <strategy_config>…</strategy_config>.
4. List every change in a <changes>…</changes> block with old → new values
   and the exact metric that motivated each change.

The output name MUST be exactly: {new_name}
"""


# ── Response parser ───────────────────────────────────────────
def extract_optimizer_response(text: str) -> tuple[Optional[dict], Optional[str]]:
    """
    Parse <strategy_config> and <changes> blocks from the optimizer's response.
    Returns (proposal_dict, changes_text). Either may be None if absent / malformed.
    """
    proposal = None
    sc_match = re.search(
        r"<strategy_config>\s*(.*?)\s*</strategy_config>",
        text, re.DOTALL,
    )
    if sc_match:
        raw = sc_match.group(1).strip()
        # Strip any accidental markdown code fences
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        try:
            proposal = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("Optimizer: could not parse strategy_config JSON: %s", exc)

    changes = None
    ch_match = re.search(
        r"<changes>\s*(.*?)\s*</changes>",
        text, re.DOTALL,
    )
    if ch_match:
        changes = ch_match.group(1).strip()

    return proposal, changes


# ── Version naming ────────────────────────────────────────────
def next_version_name(base_name: str) -> str:
    """
    Return the next available 'Base Name — Optimized vN' string.
    Strips any existing ' — Optimized vN' suffix from base_name first,
    then queries the DB to find what version numbers already exist.
    """
    # Remove existing optimized suffix so we always base off the original name
    clean = re.sub(
        r"\s*[—–-]+\s*Optimized\s+v\d+\s*$",
        "",
        base_name,
        flags=re.IGNORECASE,
    ).strip()

    try:
        from core.database.engine import get_session
        from core.database.models import Strategy

        with get_session() as s:
            rows = (
                s.query(Strategy.name)
                .filter(Strategy.name.like(f"{clean} — Optimized v%"))
                .all()
            )
        max_v = 0
        for (name,) in rows:
            m = re.search(r"Optimized v(\d+)\s*$", name, re.IGNORECASE)
            if m:
                max_v = max(max_v, int(m.group(1)))
        return f"{clean} — Optimized v{max_v + 1}"

    except Exception:
        return f"{clean} — Optimized v1"
