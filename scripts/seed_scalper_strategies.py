"""
Seed NexusTrader with the 4 multi-TF scalper strategies defined in the
uploaded .txt files:  BTC / ETH / SOL / XRP  Scalper.

Each strategy is identical in logic — only the symbol differs.

Strategy logic (from the documents):
  ENTRY (LONG only)
    Trend Filter  : EMA12(15m) > EMA55(15m)
    HTF Strength  : RSI14(15m) > 45
    Momentum      : RSI14(5m)  > 40
    Trigger       : MACD(5m)   crosses above Signal Line
    Momentum Cap  : RSI14(5m)  < 65
    (all conditions must be true — AND logic)

  EXIT
    Stop-Loss     : -1.8 % from entry
    Take-Profit   : +3.0 % → activate trailing stop of -1.0 %

  RISK SETTINGS
    Position size        : $40 per trade  (fixed USDT)
    Max concurrent       : 2
    Primary timeframe    : 1m  (checked every minute)
"""

import sys
import os

# ── Make sure we can import NexusTrader modules ───────────────
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.database.engine import get_session, init_database
from core.database.models import Strategy

# ── Shared strategy logic (same across all 4 coins) ──────────

def _make_definition(symbol: str) -> dict:
    return {
        "symbols": [symbol],
        "timeframe": "1m",          # primary loop — checked every minute
        "timeframes": ["1m", "5m", "15m"],
        "indicators": [
            {
                "name": "EMA",
                "period": 12,
                "timeframe": "15m",
                "label": "EMA12",
                "description": "12-period EMA on 15-minute chart"
            },
            {
                "name": "EMA",
                "period": 55,
                "timeframe": "15m",
                "label": "EMA55",
                "description": "55-period EMA on 15-minute chart"
            },
            {
                "name": "RSI",
                "period": 14,
                "timeframe": "15m",
                "label": "RSI14_15m",
                "description": "14-period RSI on 15-minute chart"
            },
            {
                "name": "RSI",
                "period": 14,
                "timeframe": "5m",
                "label": "RSI14_5m",
                "description": "14-period RSI on 5-minute chart"
            },
            {
                "name": "MACD",
                "fast": 12,
                "slow": 26,
                "signal": 9,
                "timeframe": "5m",
                "label": "MACD",
                "description": "MACD(12,26,9) on 5-minute chart"
            },
        ],
        "entry_long": {
            "conditions": [
                "EMA12 on 15m > EMA55 on 15m",
                "RSI14 on 15m > 45",
                "RSI14 on 5m > 40",
                "MACD on 5m crosses above Signal Line",
                "RSI14 on 5m < 65",
            ],
            "logic": "AND",
        },
        "exit_long": {
            "conditions": [
                "stop_loss 1.8%",
                "take_profit 3.0% then trailing_stop 1.0%",
            ],
            "logic": "OR",
        },
        "entry_short": {"conditions": [], "logic": "AND"},
        "exit_short":  {"conditions": [], "logic": "OR"},
        "risk": {
            "stop_loss_pct":            1.8,
            "take_profit_pct":          3.0,
            "trailing_stop_pct":        1.0,
            "trailing_stop_trigger_pct": 3.0,
            "position_size_usdt":       40.0,
            "max_concurrent_positions": 2,
            "order_type":               "market",
        },
    }


STRATEGIES = [
    {
        "symbol":      "BTC/USDT",
        "name":        "BTC Multi-TF Momentum Scalper",
        "description": (
            "Long-only BTC/USDT scalper that enters when the 15m trend is confirmed "
            "(EMA12 > EMA55), the 15m RSI is above 45, and a 5m MACD bullish crossover "
            "occurs with RSI between 40-65. Exits via a 1.8% hard stop-loss or a 3% "
            "take-profit that activates a 1% trailing stop to lock in gains. "
            "Runs on 1-minute cadence; max 2 concurrent positions; $40 per trade."
        ),
    },
    {
        "symbol":      "ETH/USDT",
        "name":        "ETH Multi-TF Momentum Scalper",
        "description": (
            "Long-only ETH/USDT scalper that enters when the 15m trend is confirmed "
            "(EMA12 > EMA55), the 15m RSI is above 45, and a 5m MACD bullish crossover "
            "occurs with RSI between 40-65. Exits via a 1.8% hard stop-loss or a 3% "
            "take-profit that activates a 1% trailing stop to lock in gains. "
            "Runs on 1-minute cadence; max 2 concurrent positions; $40 per trade."
        ),
    },
    {
        "symbol":      "SOL/USDT",
        "name":        "SOL Multi-TF Momentum Scalper",
        "description": (
            "Long-only SOL/USDT scalper that enters when the 15m trend is confirmed "
            "(EMA12 > EMA55), the 15m RSI is above 45, and a 5m MACD bullish crossover "
            "occurs with RSI between 40-65. Exits via a 1.8% hard stop-loss or a 3% "
            "take-profit that activates a 1% trailing stop to lock in gains. "
            "Runs on 1-minute cadence; max 2 concurrent positions; $40 per trade."
        ),
    },
    {
        "symbol":      "XRP/USDT",
        "name":        "XRP Multi-TF Momentum Scalper",
        "description": (
            "Long-only XRP/USDT scalper that enters when the 15m trend is confirmed "
            "(EMA12 > EMA55), the 15m RSI is above 45, and a 5m MACD bullish crossover "
            "occurs with RSI between 40-65. Exits via a 1.8% hard stop-loss or a 3% "
            "take-profit that activates a 1% trailing stop to lock in gains. "
            "Runs on 1-minute cadence; max 2 concurrent positions; $40 per trade."
        ),
    },
]


def main():
    init_database()

    saved = []
    skipped = []

    with get_session() as session:
        for spec in STRATEGIES:
            # Skip if a strategy with this exact name already exists
            existing = session.query(Strategy).filter_by(name=spec["name"]).first()
            if existing:
                skipped.append(spec["name"])
                print(f"  [skip]  Already exists: {spec['name']}")
                continue

            strat = Strategy(
                name            = spec["name"],
                description     = spec["description"],
                type            = "ai",
                status          = "draft",
                lifecycle_stage = 1,
                definition      = _make_definition(spec["symbol"]),
                ai_generated    = True,
                ai_model_used   = "claude-sonnet-4-6",
                created_by      = "ai_lab",
            )
            session.add(strat)
            session.flush()
            saved.append(f"{spec['name']}  (id={strat.id})")
            print(f"  [saved] {spec['name']}  →  id={strat.id}")

    print()
    print(f"Done.  Saved: {len(saved)}   Skipped (already exist): {len(skipped)}")
    for s in saved:
        print(f"  ✓  {s}")


if __name__ == "__main__":
    main()
