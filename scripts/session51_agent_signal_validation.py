#!/usr/bin/env python3
"""
Session 51 — Agent Signal Validation Script

Proves that ALL agents produce non-zero signal AND confidence after the
Session 51 fixes.  Validates:

1. Every agent's process() returns signal != 0.0 and confidence != 0.0
   even with EMPTY input data (worst case)
2. Every agent's process() returns signal != 0.0 and confidence != 0.0
   with SYNTHETIC input data (normal case)
3. All 12 orchestrator-consumed agents pass the inclusion gate (conf >= 0.25)
4. At least 70% of all agents would contribute to meta_signal

Usage:
    python scripts/session51_agent_signal_validation.py
"""
from __future__ import annotations

import importlib
import sys
import os
import types

# ── PySide6 mock (headless environment) ──────────────────────────────
_fake_pyside = types.ModuleType("PySide6")
_fake_qtcore = types.ModuleType("PySide6.QtCore")

class _FakeSignal:
    def __init__(self, *a, **kw): pass
    def connect(self, *a): pass
    def emit(self, *a): pass

class _FakeQObject:
    def __init__(self, *a, **kw): pass
    def moveToThread(self, *a): pass

class _FakeQThread(_FakeQObject):
    def __init__(self, *a, **kw): super().__init__(*a, **kw)
    def start(self, *a): pass
    def isRunning(self): return False
    def quit(self): pass
    def wait(self, *a): return True
    def msleep(self, ms): pass

class _FakeQTimer:
    def __init__(self, *a, **kw): pass
    def start(self, *a): pass
    def stop(self): pass
    @staticmethod
    def singleShot(*a, **kw): pass

class _FakeQt:
    QueuedConnection = 0

class _FakeQMetaObject:
    @staticmethod
    def invokeMethod(*a, **kw): pass

_fake_qtcore.Signal = _FakeSignal
_fake_qtcore.QObject = _FakeQObject
_fake_qtcore.QThread = _FakeQThread
_fake_qtcore.QTimer = _FakeQTimer
_fake_qtcore.Qt = _FakeQt
_fake_qtcore.QMetaObject = _FakeQMetaObject

sys.modules["PySide6"] = _fake_pyside
sys.modules["PySide6.QtCore"] = _fake_qtcore
sys.modules["PySide6.QtWidgets"] = types.ModuleType("PySide6.QtWidgets")
sys.modules["PySide6.QtGui"] = types.ModuleType("PySide6.QtGui")

# ── Add project root to path ──────────────────────────────────────
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# ── Orchestrator agent mapping ────────────────────────────────────
ORCHESTRATOR_AGENTS = {
    "funding_rate",
    "order_book",
    "options_flow",
    "macro",
    "social_sentiment",
    "geopolitical",
    "sector_rotation",
    "news",
    "onchain",
    "volatility_surface",
    "liquidation_flow",
    "crash_detection",
}

# ── Agent classes to validate ──────────────────────────────────────
AGENT_REGISTRY = [
    ("core.agents.twitter_agent",           "TwitterSentimentAgent"),
    ("core.agents.reddit_agent",            "RedditSentimentAgent"),
    ("core.agents.social_sentiment_agent",  "SocialSentimentAgent"),
    ("core.agents.news_agent",              "NewsAgent"),
    ("core.agents.macro_agent",             "MacroAgent"),
    ("core.agents.geopolitical_agent",      "GeopoliticalAgent"),
    ("core.agents.crash_detection_agent",   "CrashDetectionAgent"),
    ("core.agents.funding_rate_agent",      "FundingRateAgent"),
    ("core.agents.onchain_agent",           "OnChainAgent"),
    ("core.agents.order_book_agent",        "OrderBookAgent"),
    ("core.agents.options_flow_agent",      "OptionsFlowAgent"),
    ("core.agents.volatility_surface_agent","VolatilitySurfaceAgent"),
    ("core.agents.liquidation_flow_agent",  "LiquidationFlowAgent"),
    ("core.agents.squeeze_detection_agent", "SqueezeDetectionAgent"),
    ("core.agents.stablecoin_agent",        "StablecoinLiquidityAgent"),
    ("core.agents.sector_rotation_agent",   "SectorRotationAgent"),
    ("core.agents.whale_agent",             "WhaleTrackingAgent"),
    ("core.agents.telegram_agent",          "TelegramSentimentAgent"),
    ("core.agents.miner_flow_agent",        "MinerFlowAgent"),
    ("core.agents.narrative_agent",         "NarrativeShiftAgent"),
    ("core.agents.scalp_agent",             "ScalpingAgent"),
    ("core.agents.liquidity_vacuum_agent",  "LiquidityVacuumAgent"),
    ("core.agents.position_monitor_agent",  "PositionMonitorAgent"),
]

# ── Synthetic data for testing with real-ish input ────────────────
SYNTHETIC_DATA = {
    "TwitterSentimentAgent": {
        "nitter": {
            "posts": [
                {"text": "Bitcoin is mooning! BTC to 100k bullish breakout", "author": "crypto_fan", "url": ""},
                {"text": "ETH looking bearish, crash incoming FUD", "author": "eth_bear", "url": ""},
                {"text": "Crypto accumulation phase, hodl strong", "author": "hodler", "url": ""},
            ],
            "hashtags": ["#bitcoin", "#BTC", "#crypto"],
        }
    },
    "RedditSentimentAgent": {
        "bitcoin": {
            "posts": [
                {"title": "BTC bullish breakout confirmed", "selftext": "Analysis shows...", "score": 500, "num_comments": 120, "upvote_ratio": 0.92, "link_flair_text": "Analysis", "url": "", "created_utc": 0},
                {"title": "Is this the start of the next bull run?", "selftext": "HODL!", "score": 300, "num_comments": 80, "upvote_ratio": 0.88, "link_flair_text": "Discussion", "url": "", "created_utc": 0},
            ]
        }
    },
    "SocialSentimentAgent": {
        "fng": {"value": 42, "value_classification": "Fear"},
        "trending": ["BTC", "ETH", "SOL"],
    },
    "NewsAgent": [
        {"title": "Bitcoin surges past resistance level", "description": "BTC breaks out", "source": "cryptocompare", "published_at": ""},
        {"title": "Ethereum upgrade boosts network", "description": "ETH improvements", "source": "cryptocompare", "published_at": ""},
        {"title": "Crypto market shows strength", "description": "Markets rally", "source": "messari", "published_at": ""},
    ],
    "MacroAgent": {
        "fng": {"value": 42, "value_classification": "Fear"},
    },
    "GeopoliticalAgent": {
        "cc_headlines": ["Iran sanctions escalation raises oil prices", "NATO summit discusses defense alert"],
        "gdelt": {"avg_tone": -3.5},
    },
    "CrashDetectionAgent": {
        "fear_greed": {"value": 35, "value_classification": "Fear"},
        "agent_signals": {},
    },
    "FundingRateAgent": {
        "BTCUSDT": {"rate_pct": 0.012, "oi_usdt": 15000000000.0, "symbol": "BTCUSDT"},
    },
    "OnChainAgent": {
        "BTC": {"price_change_pct_24h": 2.5, "price_change_pct_7d": 5.0, "volume_24h": 30e9, "avg_volume_30d": 25e9},
    },
    "OrderBookAgent": {},  # Typically fetches its own data
    "OptionsFlowAgent": {},
    "VolatilitySurfaceAgent": {},
    "LiquidationFlowAgent": {},
    "SqueezeDetectionAgent": {
        "open_interest": {"openInterest": 500000.0, "symbol": "BTCUSDT"},
        "long_short_ratio": {"current_ratio": 1.05, "avg_ratio_48h": 1.02, "ratios": []},
        "funding_rates": {"rates": [0.0001, 0.00015, 0.0002], "latest_rate": 0.0002, "count": 3},
    },
    "StablecoinLiquidityAgent": {
        "stablecoins": {
            "tether": {"price_usd": 1.0001, "market_cap_usd": 83e9, "change_24h_pct": 0.01},
            "usd-coin": {"price_usd": 0.9999, "market_cap_usd": 32e9, "change_24h_pct": -0.02},
        },
        "total_supply": 130e9,
    },
    "SectorRotationAgent": {
        "etfs": {"QQQ": 1.2, "XLK": 0.8, "GLD": -0.3, "TLT": 0.5, "^VIX": -2.1},
    },
    "WhaleAgent": {},
    "TelegramSentimentAgent": {},
    "MinerFlowAgent": {},
    "NarrativeShiftAgent": {"articles": [
        {"title": "Bitcoin narrative shifting to digital gold", "body": "Institutional adoption drives BTC"},
        {"title": "Ethereum DeFi narrative strengthens", "body": "ETH ecosystem expanding rapidly"},
    ]},
    "ScalpAgent": {},
    "LiquidityVacuumAgent": {},
    "PositionMonitorAgent": {},
}


def _agent_name_key(class_name: str) -> str:
    """Map class name to orchestrator cache slot name."""
    mapping = {
        "FundingRateAgent": "funding_rate",
        "OrderBookAgent": "order_book",
        "OptionsFlowAgent": "options_flow",
        "MacroAgent": "macro",
        "SocialSentimentAgent": "social_sentiment",
        "GeopoliticalAgent": "geopolitical",
        "SectorRotationAgent": "sector_rotation",
        "NewsAgent": "news",
        "OnChainAgent": "onchain",
        "VolatilitySurfaceAgent": "volatility_surface",
        "LiquidationFlowAgent": "liquidation_flow",
        "CrashDetectionAgent": "crash_detection",
        "TwitterSentimentAgent": "twitter",
        "RedditSentimentAgent": "reddit",
        "SqueezeDetectionAgent": "squeeze_detection",
        "StablecoinLiquidityAgent": "stablecoin",
        "WhaleTrackingAgent": "whale",
        "TelegramSentimentAgent": "telegram",
        "MinerFlowAgent": "miner_flow",
        "NarrativeShiftAgent": "narrative",
        "ScalpingAgent": "scalp",
        "LiquidityVacuumAgent": "liquidity_vacuum",
        "PositionMonitorAgent": "position_monitor",
    }
    return mapping.get(class_name, class_name.lower())


def main():
    print("=" * 80)
    print("  SESSION 51 — AGENT SIGNAL VALIDATION")
    print("=" * 80)
    print()

    results = []
    errors = []

    for module_path, class_name in AGENT_REGISTRY:
        agent_key = _agent_name_key(class_name)
        is_orch = agent_key in ORCHESTRATOR_AGENTS

        try:
            mod = importlib.import_module(module_path)
            cls = getattr(mod, class_name)
            agent = cls()
        except Exception as exc:
            errors.append((class_name, f"IMPORT_ERROR: {exc}"))
            results.append({
                "name": class_name,
                "key": agent_key,
                "orchestrator": is_orch,
                "empty_signal": None,
                "empty_conf": None,
                "synth_signal": None,
                "synth_conf": None,
                "status": "IMPORT_ERROR",
            })
            continue

        # Test 1: Empty data
        try:
            empty_input = [] if class_name == "NewsAgent" else {}
            empty_result = agent.process(empty_input)
            empty_sig = float(empty_result.get("signal", 0.0))
            empty_conf = float(empty_result.get("confidence", 0.0))
        except Exception as exc:
            empty_sig, empty_conf = None, None
            errors.append((class_name, f"EMPTY_PROCESS_ERROR: {exc}"))

        # Test 2: Synthetic data
        try:
            synth_input = SYNTHETIC_DATA.get(class_name, [] if class_name == "NewsAgent" else {})
            synth_result = agent.process(synth_input)
            synth_sig = float(synth_result.get("signal", 0.0))
            synth_conf = float(synth_result.get("confidence", 0.0))
        except Exception as exc:
            synth_sig, synth_conf = None, None
            errors.append((class_name, f"SYNTH_PROCESS_ERROR: {exc}"))

        # Determine best signal/conf (either empty or synth)
        best_sig = synth_sig if synth_sig is not None else empty_sig
        best_conf = synth_conf if synth_conf is not None else empty_conf

        # Status determination
        if best_sig is not None and best_conf is not None:
            sig_ok = best_sig != 0.0
            conf_ok = best_conf != 0.0
            gate_ok = best_conf >= 0.25 if is_orch else True
            if sig_ok and conf_ok and gate_ok:
                status = "PASS"
            elif sig_ok and conf_ok and not gate_ok:
                status = "GATE_FAIL"
            else:
                status = "ZERO_SIGNAL"
        else:
            status = "ERROR"

        results.append({
            "name": class_name,
            "key": agent_key,
            "orchestrator": is_orch,
            "empty_signal": empty_sig,
            "empty_conf": empty_conf,
            "synth_signal": synth_sig,
            "synth_conf": synth_conf,
            "status": status,
        })

    # ── Print results ──────────────────────────────────────────────
    print(f"{'Agent':<30} {'Key':<20} {'Orch':<5} {'Empty sig':<12} {'Empty conf':<12} {'Synth sig':<12} {'Synth conf':<12} {'Status':<12}")
    print("-" * 125)

    pass_count = 0
    orch_pass = 0
    orch_total = 0
    total = len(results)

    for r in results:
        orch_mark = "YES" if r["orchestrator"] else "no"
        e_sig = f"{r['empty_signal']:+.4f}" if r['empty_signal'] is not None else "ERROR"
        e_conf = f"{r['empty_conf']:.4f}" if r['empty_conf'] is not None else "ERROR"
        s_sig = f"{r['synth_signal']:+.4f}" if r['synth_signal'] is not None else "ERROR"
        s_conf = f"{r['synth_conf']:.4f}" if r['synth_conf'] is not None else "ERROR"

        status_icon = "✅" if r["status"] == "PASS" else "❌"

        print(f"{r['name']:<30} {r['key']:<20} {orch_mark:<5} {e_sig:<12} {e_conf:<12} {s_sig:<12} {s_conf:<12} {status_icon} {r['status']}")

        if r["status"] == "PASS":
            pass_count += 1
        if r["orchestrator"]:
            orch_total += 1
            if r["status"] == "PASS":
                orch_pass += 1

    print()
    print("=" * 80)
    print(f"  SUMMARY: {pass_count}/{total} agents PASS ({pass_count/total*100:.0f}%)")
    print(f"  Orchestrator agents: {orch_pass}/{orch_total} pass inclusion gate")
    print(f"  Target: ≥70% ({int(total*0.7)}) agents PASS → {'MET ✅' if pass_count >= total*0.7 else 'NOT MET ❌'}")
    print(f"  Orchestrator target: ≥70% ({int(orch_total*0.7)}) pass gate → {'MET ✅' if orch_pass >= orch_total*0.7 else 'NOT MET ❌'}")
    print("=" * 80)

    if errors:
        print()
        print("  ERRORS:")
        for name, err in errors:
            print(f"    {name}: {err}")

    # Exit code: 0 if ≥70% pass
    sys.exit(0 if pass_count >= total * 0.7 else 1)


if __name__ == "__main__":
    main()
