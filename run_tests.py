#!/usr/bin/env python3
"""
NexusTrader -- Manual Test Plan
================================
Run from the project root on Windows:

    cd C:/path/to/NexusTrader
    python run_tests.py

Options:
    --section N   Run only section N (1-11)
    --fast        Skip tests that hit the network or scan all .py files
    --verbose     Show full tracebacks on failure
"""

from __future__ import annotations
import argparse, importlib, os, sys, time, traceback
from pathlib import Path
from typing import Callable

# ── Bootstrap sys.path ───────────────────────────────────────
ROOT = Path(__file__).parent.resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

# ── CLI args ─────────────────────────────────────────────────
ap = argparse.ArgumentParser(description="NexusTrader Test Plan")
ap.add_argument("--section", type=int, default=0)
ap.add_argument("--fast",    action="store_true")
ap.add_argument("--verbose", action="store_true")
ARGS = ap.parse_args()

# ── Output helpers ────────────────────────────────────────────
try:
    import colorama; colorama.init(autoreset=True)
    GRN = colorama.Fore.GREEN;  RED = colorama.Fore.RED
    YLW = colorama.Fore.YELLOW; CYN = colorama.Fore.CYAN
    DIM = colorama.Style.DIM;   RST = colorama.Style.RESET_ALL
    BLD = colorama.Style.BRIGHT
except ImportError:
    GRN = RED = YLW = CYN = DIM = RST = BLD = ""

PASS_COUNT = FAIL_COUNT = SKIP_COUNT = 0
FAILS: list[tuple[str, str]] = []


def _hdr(num: int, title: str):
    print(f"\n{BLD}{CYN}{'─'*60}{RST}")
    print(f"{BLD}{CYN}  Section {num}: {title}{RST}")
    print(f"{BLD}{CYN}{'─'*60}{RST}")


def _ok(name: str, detail: str = ""):
    global PASS_COUNT
    PASS_COUNT += 1
    print(f"{GRN}  PASS{RST}  {name}" + (f"  {DIM}({detail}){RST}" if detail else ""))


def _fail(name: str, reason: str):
    global FAIL_COUNT
    FAIL_COUNT += 1
    FAILS.append((name, reason))
    short = reason.splitlines()[-1][:110] if reason else "unknown"
    print(f"{RED}  FAIL{RST}  {name}  {DIM}-> {short}{RST}")
    if ARGS.verbose:
        print(f"{DIM}{reason}{RST}")


def _skip(name: str, why: str = ""):
    global SKIP_COUNT
    SKIP_COUNT += 1
    print(f"{YLW}  SKIP{RST}  {name}" + (f"  {DIM}({why}){RST}" if why else ""))


def test(name: str, fn: Callable, *, skip: bool = False, skip_reason: str = ""):
    if skip:
        _skip(name, skip_reason); return
    try:
        t0 = time.perf_counter()
        result = fn()
        ms = (time.perf_counter() - t0) * 1000
        detail = f"{ms:.0f}ms"
        if isinstance(result, str):
            detail += f" | {result}"
        _ok(name, detail)
    except Exception as exc:
        _fail(name, traceback.format_exc() if ARGS.verbose else str(exc))


def should_run(section: int) -> bool:
    return ARGS.section == 0 or ARGS.section == section


# ═══════════════════════════════════════════════════════════════
# SECTION 1 — Environment & Dependencies
# ═══════════════════════════════════════════════════════════════
if should_run(1):
    _hdr(1, "Environment & Dependencies")

    def check_python():
        v = sys.version_info
        assert v >= (3, 11), f"Python 3.11+ required, got {v.major}.{v.minor}"
        return f"{v.major}.{v.minor}.{v.micro}"
    test("Python >= 3.11", check_python)

    for pkg in [
        "PySide6", "pandas", "numpy", "sqlalchemy", "yaml",
        "cryptography", "requests", "feedparser", "vaderSentiment",
        "ccxt", "arch", "hmmlearn", "gymnasium", "safetensors",
    ]:
        def _chk(p=pkg):
            mod = importlib.import_module(p)
            return getattr(mod, "__version__", "installed")
        test(f"Package: {pkg}", _chk)

    def check_torch():
        import torch
        cuda = "CUDA OK" if torch.cuda.is_available() else "CPU only"
        return f"{torch.__version__} | {cuda}"
    test("Package: torch + CUDA", check_torch)

    def check_transformers():
        import transformers
        return transformers.__version__
    test("Package: transformers", check_transformers)

    def check_syntax():
        import ast
        errors = []
        for p in ROOT.rglob("*.py"):
            if "__pycache__" in str(p): continue
            try:
                ast.parse(p.read_text(encoding="utf-8", errors="ignore"))
            except SyntaxError as e:
                errors.append(f"{p.relative_to(ROOT)}:{e.lineno}: {e.msg}")
        assert not errors, "\n".join(errors)
        n = sum(1 for _ in ROOT.rglob("*.py") if "__pycache__" not in str(_))
        return f"{n} files OK"
    test("Syntax: all .py files", check_syntax, skip=ARGS.fast, skip_reason="--fast")


# ═══════════════════════════════════════════════════════════════
# SECTION 2 — Core Infrastructure
# ═══════════════════════════════════════════════════════════════
if should_run(2):
    _hdr(2, "Core Infrastructure")

    def check_settings():
        from config.settings import settings
        val = settings.get("app.theme", "dark")
        assert val in ("dark", "light")
        return f"theme={val}"
    test("Settings: load config.yaml", check_settings)

    def check_settings_roundtrip():
        from config.settings import settings
        settings.set("_test.value", 42)
        assert settings.get("_test.value") == 42
        settings.set("_test.value", None)
    test("Settings: set/get round-trip", check_settings_roundtrip)

    def check_vault():
        from core.security.key_vault import key_vault
        key_vault.save("_test.key", "test_value_xyz")
        val = key_vault.load("_test.key")
        assert val == "test_value_xyz", f"got {val!r}"
        key_vault.save("_test.key", "")
        return "encrypt/decrypt OK"
    test("KeyVault: encrypt/decrypt round-trip", check_vault)

    def check_event_bus():
        from core.event_bus import bus, Topics
        received = []
        def handler(event): received.append(event.data)
        bus.subscribe(Topics.STATUS_UPDATE, handler)
        bus.publish(Topics.STATUS_UPDATE, {"msg": "test"}, source="test")
        time.sleep(0.05)
        bus.unsubscribe(Topics.STATUS_UPDATE, handler)
        assert any(d.get("msg") == "test" for d in received)
        return "pub/sub OK"
    test("EventBus: publish/subscribe cycle", check_event_bus)

    def check_topics():
        from core.event_bus import Topics
        required = [
            "NOTIFICATION_SENT", "ORCHESTRATOR_SIGNAL", "SOCIAL_SIGNAL",
            "TRADE_OPENED", "TRADE_CLOSED", "FEED_STATUS", "REGIME_CHANGED",
            "AGENT_SIGNAL", "SIGNAL_CONFIRMED", "CANDIDATE_APPROVED",
        ]
        missing = [t for t in required if not hasattr(Topics, t)]
        assert not missing, f"Missing: {missing}"
        n = len([a for a in dir(Topics) if not a.startswith("_")])
        return f"{n} topics defined"
    test("EventBus: required Topics present", check_topics)

    def check_database():
        from core.database.engine import get_session, init_database
        init_database()
        with get_session() as s:
            from core.database.models import Strategy
            count = s.query(Strategy).count()
        return f"{count} strategies in DB"
    test("Database: init and query", check_database)

    def check_db_crud():
        from core.database.engine import get_session, init_database
        from core.database.models import Strategy
        init_database()
        name = "__test_delete_me__"
        with get_session() as s:
            obj = Strategy(name=name, type="rule", definition={})
            s.add(obj); s.flush()
            sid = obj.id
        with get_session() as s:
            assert s.get(Strategy, sid) is not None
            s.delete(s.get(Strategy, sid))
        with get_session() as s:
            assert s.get(Strategy, sid) is None
        return "create/read/delete OK"
    test("Database: CRUD Strategy", check_db_crud)


# ═══════════════════════════════════════════════════════════════
# SECTION 3 — Data & Exchange Layer
# ═══════════════════════════════════════════════════════════════
if should_run(3):
    _hdr(3, "Data & Exchange Layer")

    def check_exchange_manager():
        from core.market_data.exchange_manager import ExchangeManager
        mgr = ExchangeManager()
        assert hasattr(mgr, "get_exchange")
        return "ExchangeManager OK"
    test("ExchangeManager: import and init", check_exchange_manager)

    def check_historical_loader():
        from core.market_data.historical_loader import HistoricalLoaderWorker
        # QThread worker — requires symbol/timeframe args, no default ctor
        worker = HistoricalLoaderWorker(symbol="BTC/USDT", timeframe="1h", days_back=30)
        assert hasattr(worker, "run")
        assert hasattr(worker, "stop")
        return "HistoricalLoaderWorker OK"
    test("HistoricalLoader: import and init", check_historical_loader)

    def check_data_feed():
        from core.market_data.data_feed import LiveDataFeed
        assert hasattr(LiveDataFeed, "start") or hasattr(LiveDataFeed, "_run_rest_loop")
        return "LiveDataFeed importable"
    test("LiveDataFeed: import (no start)", check_data_feed)

    def check_indicators():
        import pandas as pd, numpy as np
        from core.features.indicator_library import calculate_all
        idx = pd.date_range("2024-01-01", periods=50, freq="1h")
        df = pd.DataFrame({
            "open":  np.random.uniform(40000, 45000, 50),
            "high":  np.random.uniform(44000, 46000, 50),
            "low":   np.random.uniform(39000, 41000, 50),
            "close": np.random.uniform(40000, 45000, 50),
            "volume":np.random.uniform(100, 500, 50),
        }, index=idx)
        result = calculate_all(df)
        assert isinstance(result, pd.DataFrame)
        return f"{len(result.columns)} indicators"
    test("IndicatorLibrary: calculate_all on synthetic OHLCV", check_indicators)

    def check_watchlist():
        from core.scanning.watchlist import WatchlistManager
        wl = WatchlistManager()
        syms = wl.get_active_symbols()
        assert isinstance(syms, list)
        return f"{len(syms)} symbols"
    test("WatchlistManager: get_active_symbols", check_watchlist)


# ═══════════════════════════════════════════════════════════════
# SECTION 4 — Agent Layer
# ═══════════════════════════════════════════════════════════════
if should_run(4):
    _hdr(4, "Agent Layer")

    agents = [
        ("FundingRateAgent",        "core.agents.funding_rate_agent",      "FundingRateAgent"),
        ("SocialSentimentAgent",    "core.agents.social_sentiment_agent",  "SocialSentimentAgent"),
        ("MacroAgent",              "core.agents.macro_agent",             "MacroAgent"),
        ("WhaleTrackingAgent",      "core.agents.whale_agent",             "WhaleTrackingAgent"),
        ("OnChainAgent",            "core.agents.onchain_agent",           "OnChainAgent"),
        ("MinerFlowAgent",          "core.agents.miner_flow_agent",        "MinerFlowAgent"),
        ("StablecoinLiquidityAgent","core.agents.stablecoin_agent",        "StablecoinLiquidityAgent"),
        ("GeopoliticalAgent",       "core.agents.geopolitical_agent",      "GeopoliticalAgent"),
        ("SectorRotationAgent",     "core.agents.sector_rotation_agent",   "SectorRotationAgent"),
        ("SqueezeDetectionAgent",   "core.agents.squeeze_detection_agent", "SqueezeDetectionAgent"),
        ("PositionMonitorAgent",    "core.agents.position_monitor_agent",  "PositionMonitorAgent"),
    ]
    for label, module, cls_name in agents:
        def _import(m=module, c=cls_name):
            mod = importlib.import_module(m)
            cls = getattr(mod, c)
            assert callable(cls)
            return "importable"
        test(f"Agent import: {label}", _import)

    def check_coordinator():
        from core.agents.agent_coordinator import get_coordinator
        coord = get_coordinator()
        assert hasattr(coord, "start_all")
        return "AgentCoordinator OK"
    test("AgentCoordinator: get_coordinator", check_coordinator)

    def check_orchestrator():
        from core.orchestrator.orchestrator_engine import OrchestratorEngine
        eng = OrchestratorEngine()
        # get_signal() is the method on this implementation
        assert hasattr(eng, "get_signal") or hasattr(eng, "_recalculate")
        return "OrchestratorEngine init OK"
    test("OrchestratorEngine: import and init", check_orchestrator)

    def check_social_process():
        from core.agents.social_sentiment_agent import SocialSentimentAgent
        agent = SocialSentimentAgent()
        raw = {
            "fng": {"value": 72, "value_classification": "Greed"},
            "twitter": {"signal": 0.4, "confidence": 0.7},
            "reddit":  {"signal": 0.2, "confidence": 0.5},
        }
        result = agent.process(raw)
        assert "signal" in result and "confidence" in result
        assert -1.0 <= result["signal"] <= 1.0
        return f"signal={result['signal']:+.3f} label={result['sentiment_label']}"
    test("SocialSentimentAgent: process() aggregation", check_social_process)


# ═══════════════════════════════════════════════════════════════
# SECTION 5 — Signal Pipeline
# ═══════════════════════════════════════════════════════════════
if should_run(5):
    _hdr(5, "Signal Pipeline")

    def check_sg_init():
        from core.signals.signal_generator import SignalGenerator
        sg = SignalGenerator()
        assert hasattr(sg, "generate")
        assert hasattr(sg, "reset_warmup")
        assert sg._warmup_bars_remaining == 100
        return "warmup=100 bars"
    test("SignalGenerator: init + warmup guard", check_sg_init)

    def check_sg_warmup():
        import pandas as pd, numpy as np
        from core.signals.signal_generator import SignalGenerator
        sg = SignalGenerator()
        sg.reset_warmup(5)
        idx = pd.date_range("2024-01-01", periods=3, freq="1h")
        df = pd.DataFrame({
            "open": [42000.0]*3, "high": [43000.0]*3,
            "low":  [41000.0]*3, "close":[42000.0]*3,
            "volume":[100.0]*3,
        }, index=idx)
        # generate() signature: (symbol, df, regime, timeframe)
        result = sg.generate("BTC/USDT", df, "bull_trend", "1h")
        assert result == [], f"warmup should suppress signals, got {result}"
        return "warmup suppresses signals"
    test("SignalGenerator: warmup suppresses signals", check_sg_warmup)

    def check_custom_rule():
        import pandas as pd, numpy as np
        from core.signals.sub_models.custom_rule_model import CustomRuleModel
        tree = {"type": "AND", "children": [
            {"type": "leaf", "left": "close", "op": ">", "right": 40000}
        ]}
        model = CustomRuleModel(
            entry_long_tree=tree, entry_short_tree=None,
            stop_loss_pct=2.0, take_profit_pct=4.0,
            timeframe="1h", rule_name="test_rule",
        )
        idx = pd.date_range("2024-01-01", periods=10, freq="1h")
        df = pd.DataFrame({
            "open": [42000.0]*10, "high": [43000.0]*10,
            "low":  [41000.0]*10, "close":[45000.0]*10,
            "volume":[100.0]*10,
        }, index=idx)
        signals = model.generate("BTC/USDT", df, timeframe="1h")
        assert isinstance(signals, list)
        return f"{len(signals)} signal(s)"
    test("CustomRuleModel: condition tree evaluation", check_custom_rule)

    def check_confluence():
        from config.settings import settings
        settings.set("rl.enabled", False)
        from core.meta_decision.confluence_scorer import ConfluenceScorer
        cs = ConfluenceScorer()
        assert "orchestrator" in cs._weights, "orchestrator weight missing"
        assert cs._weights.get("rl_ensemble", -1) == 0.0, "rl_ensemble should be 0"
        return f"orchestrator={cs._weights['orchestrator']} rl=0"
    test("ConfluenceScorer: orchestrator weight + RL zeroed", check_confluence)

    def check_order_candidate():
        from core.meta_decision.order_candidate import OrderCandidate
        c = OrderCandidate(
            symbol="BTC/USDT", side="buy", entry_type="market",
            entry_price=42000.0, stop_loss_price=41160.0,
            take_profit_price=43680.0, position_size_usdt=100.0,
            score=0.72, models_fired=["trend", "momentum_breakout"],
            regime="bull_trend", rationale="test", timeframe="1h", atr_value=500.0,
        )
        assert c.risk_reward_ratio > 0
        d = c.to_dict()
        assert d["symbol"] == "BTC/USDT"
        return f"R:R={c.risk_reward_ratio}"
    test("OrderCandidate: creation + R:R computation", check_order_candidate)

    def check_risk_gate():
        from core.risk.risk_gate import RiskGate
        from core.meta_decision.order_candidate import OrderCandidate
        # RiskGate(max_concurrent_positions, max_portfolio_drawdown_pct, ...)
        gate = RiskGate(
            max_concurrent_positions=5,
            max_portfolio_drawdown_pct=20.0,
            min_risk_reward=1.5,
            max_spread_pct=0.5,
        )
        c = OrderCandidate(
            symbol="BTC/USDT", side="buy", entry_type="market",
            entry_price=42000.0, stop_loss_price=41160.0,
            take_profit_price=43680.0, position_size_usdt=100.0,
            score=0.72, models_fired=["trend"],
            regime="bull_trend", rationale="test", timeframe="1h", atr_value=500.0,
        )
        approved, rejected = gate.validate_batch(
            [c], open_positions=[], available_capital_usdt=1000.0, portfolio_drawdown_pct=0.0
        )
        return f"approved={len(approved)} rejected={len(rejected)}"
    test("RiskGate: validate_batch (no crash)", check_risk_gate)


# ═══════════════════════════════════════════════════════════════
# SECTION 6 — Execution Layer
# ═══════════════════════════════════════════════════════════════
if should_run(6):
    _hdr(6, "Execution Layer")

    def check_pe_init():
        from core.execution.paper_executor import PaperExecutor
        pe = PaperExecutor(initial_capital_usdt=500.0)
        # _initial_capital always equals the constructor arg regardless of DB history
        assert pe._initial_capital == 500.0, f"initial={pe._initial_capital}"
        # drawdown is always non-negative; may be >0 if prior test runs left DB trades
        assert pe.drawdown_pct >= 0.0, f"drawdown={pe.drawdown_pct}"
        return f"capital={pe._initial_capital}"
    test("PaperExecutor: init with capital", check_pe_init)

    def check_pe_slippage():
        from core.execution.paper_executor import PaperExecutor
        pe = PaperExecutor(500.0)
        assert pe._SLIPPAGE_MIN == 0.0005
        assert pe._SLIPPAGE_MAX == 0.0015
        assert pe._SPREAD_HALF  == 0.0005
        for _ in range(20):
            assert pe._apply_slippage(10000.0, "buy")  > 10000.0
            assert pe._apply_slippage(10000.0, "sell") < 10000.0
        return "slippage always correct direction"
    test("PaperExecutor: slippage applied to both sides", check_pe_slippage)

    def check_pe_submit():
        from core.execution.paper_executor import PaperExecutor
        from core.meta_decision.order_candidate import OrderCandidate
        pe = PaperExecutor(500.0)
        c = OrderCandidate(
            symbol="BTC/USDT", side="buy", entry_type="market",
            entry_price=42000.0, stop_loss_price=41160.0,
            take_profit_price=43680.0, position_size_usdt=50.0,
            score=0.75, models_fired=["trend"], regime="bull_trend",
            rationale="test", timeframe="1h", atr_value=500.0, approved=True,
        )
        ok = pe.submit(c)
        positions = pe.get_open_positions()
        assert ok, "submit() should return True"
        assert len(positions) == 1
        return f"opened {positions[0]['symbol']} size_usdt=${positions[0]['size_usdt']:.0f}"
    test("PaperExecutor: submit + open position", check_pe_submit)

    def check_pe_close():
        from core.execution.paper_executor import PaperExecutor
        from core.meta_decision.order_candidate import OrderCandidate
        pe = PaperExecutor(500.0)
        c = OrderCandidate(
            symbol="SOL/USDT", side="buy", entry_type="market",
            entry_price=150.0, stop_loss_price=147.0,
            take_profit_price=156.0, position_size_usdt=50.0,
            score=0.70, models_fired=["trend"], regime="bull_trend",
            rationale="test", timeframe="1h", atr_value=2.0, approved=True,
        )
        pe.submit(c)
        assert len(pe.get_open_positions()) == 1
        pe.close_position("SOL/USDT", price=156.0)
        assert len(pe.get_open_positions()) == 0
        history = pe.get_closed_trades()
        assert len(history) >= 1
        return f"closed OK, {len(history)} in history"
    test("PaperExecutor: close position + history", check_pe_close)

    def check_router():
        from core.execution.order_router import order_router, get_router
        assert order_router.mode == "paper"
        assert get_router() is order_router
        assert order_router.active_executor is not None
        return f"mode={order_router.mode}"
    test("OrderRouter: paper mode + get_router() alias", check_router)

    def check_strategy_runner():
        from core.strategies.strategy_runner import get_strategy_runner
        runner = get_strategy_runner()
        tree = {"type": "AND", "children": [
            {"type": "leaf", "left": "close", "op": ">", "right": 1}
        ]}
        ok = runner.load_strategy({
            "name": "TestStrategy", "entry_long": tree,
            "stop_loss_pct": 2.0, "take_profit_pct": 4.0, "timeframe": "1h",
        })
        assert ok
        runner.deactivate()
        return "strategy loaded + deactivated"
    test("StrategyRunner: load_strategy with condition tree", check_strategy_runner)


# ═══════════════════════════════════════════════════════════════
# SECTION 7 — Regime Detection
# ═══════════════════════════════════════════════════════════════
if should_run(7):
    _hdr(7, "Regime Detection")

    def check_rc_init():
        from core.regime.regime_classifier import RegimeClassifier
        rc = RegimeClassifier()
        assert rc._hysteresis_bars == 3
        assert hasattr(rc, "_regime_buffer")
        assert hasattr(rc, "_committed_regime")
        return f"hysteresis={rc._hysteresis_bars} bars"
    test("RegimeClassifier: init + hysteresis buffer", check_rc_init)

    def check_rc_classify():
        import pandas as pd, numpy as np
        from core.regime.regime_classifier import RegimeClassifier
        rc = RegimeClassifier()
        # classify(df) -> (regime_str, confidence, extras_dict)
        idx = pd.date_range("2024-01-01", periods=50, freq="1h")
        prices = np.linspace(40000, 45000, 50)
        df = pd.DataFrame({
            "open":  prices * 0.999, "high": prices * 1.002,
            "low":   prices * 0.997, "close": prices,
            "volume": [200.0]*50,
        }, index=idx)
        result = rc.classify(df)
        assert isinstance(result, tuple) and len(result) >= 2
        regime, confidence = result[0], result[1]
        assert isinstance(regime, str)
        assert 0.0 <= confidence <= 1.0
        return f"regime={regime} conf={confidence:.2f}"
    test("RegimeClassifier: classify() returns valid regime", check_rc_classify)

    def check_ensemble():
        from core.regime.ensemble_regime_classifier import EnsembleRegimeClassifier
        erc = EnsembleRegimeClassifier()
        assert hasattr(erc, "classify")
        return "EnsembleRegimeClassifier OK"
    test("EnsembleRegimeClassifier: import and init", check_ensemble)


# ═══════════════════════════════════════════════════════════════
# SECTION 8 — News & Sentiment
# ═══════════════════════════════════════════════════════════════
if should_run(8):
    _hdr(8, "News & Sentiment")

    def check_vader():
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
        sia = SentimentIntensityAnalyzer()
        # Use a strongly positive headline
        scores = sia.polarity_scores("Incredible gains! Bitcoin rocket to the moon!")
        assert scores["compound"] > 0, f"expected positive compound, got {scores}"
        neutral = sia.polarity_scores("The price of bitcoin is 42000 today")
        return f"positive={scores['compound']:+.2f} neutral={neutral['compound']:+.2f}"
    test("VADER: sentiment scoring", check_vader)

    def check_sentiment_engine():
        from core.sentiment.sentiment_engine import SentimentEngine
        se = SentimentEngine()
        assert hasattr(se, "fetch_and_score")
        return "SentimentEngine OK"
    test("SentimentEngine: import and init", check_sentiment_engine)

    def check_news_fetcher():
        from core.sentiment.news_fetcher import fetch_crypto_news, _fetch_cryptopanic
        assert callable(fetch_crypto_news)
        assert callable(_fetch_cryptopanic)
        return "importable"
    test("NewsFetcher: import", check_news_fetcher)

    def check_newsapi_gate():
        from core.sentiment.news_fetcher import _fetch_newsapi
        # All invalid keys must be silently rejected (no WARNING emitted)
        assert _fetch_newsapi(api_key="bad") == []
        assert _fetch_newsapi(api_key="") == []
        assert _fetch_newsapi(api_key=None) == []
        assert _fetch_newsapi(api_key="short") == []
        return "invalid keys silently gated"
    test("NewsFetcher: invalid NewsAPI keys silently gated", check_newsapi_gate)

    def check_cryptopanic():
        from core.sentiment.news_fetcher import _fetch_cryptopanic
        articles = _fetch_cryptopanic(api_key="free", symbol=None, page_size=5)
        assert isinstance(articles, list)
        if articles:
            assert "title" in articles[0]
        return f"{len(articles)} articles from CryptoPanic"
    test("NewsFetcher: CryptoPanic free-tier live fetch", check_cryptopanic,
         skip=ARGS.fast, skip_reason="--fast (network)")


# ═══════════════════════════════════════════════════════════════
# SECTION 9 — Backtesting & Strategy Lab
# ═══════════════════════════════════════════════════════════════
if should_run(9):
    _hdr(9, "Backtesting & Strategy Lab")

    def check_backtester_init():
        from core.backtesting.idss_backtester import IDSSBacktester
        bt = IDSSBacktester(warmup_bars=50)
        assert bt._warmup_bars == 50
        assert hasattr(bt, "run")
        return "warmup_bars=50"
    test("IDSSBacktester: init + warmup_bars param", check_backtester_init)

    def check_backtester_run():
        import pandas as pd, numpy as np
        from core.backtesting.idss_backtester import IDSSBacktester
        bt = IDSSBacktester(warmup_bars=10)
        idx = pd.date_range("2024-01-01", periods=60, freq="1h")
        prices = np.linspace(40000, 44000, 60) + np.random.normal(0, 200, 60)
        df = pd.DataFrame({
            "open":  prices * 0.999, "high": prices * 1.003,
            "low":   prices * 0.997, "close": prices,
            "volume": np.random.uniform(50, 200, 60),
        }, index=idx)
        result = bt.run(df, symbol="BTC/USDT", timeframe="1h", spread_pct=0.05)
        assert isinstance(result, dict)
        return f"keys: {list(result.keys())[:4]}"
    test("IDSSBacktester: minimal synthetic run", check_backtester_run)

    def check_parser_import():
        from core.strategies.ai_lab.strategy_parser import get_strategy_parser, STRATEGY_SYSTEM_PROMPT
        parser = get_strategy_parser()
        assert hasattr(parser, "parse")
        # STRATEGY_SYSTEM_PROMPT is module-level; accessible via get_system_prompt()
        prompt = parser.get_system_prompt()
        assert len(prompt) > 100, "system prompt unexpectedly short"
        return f"STRATEGY_SYSTEM_PROMPT present ({len(prompt)} chars)"
    test("StrategyParser: import + SYSTEM_PROMPT", check_parser_import)

    def check_parser_parse():
        import json
        from core.strategies.ai_lab.strategy_parser import StrategyParser
        parser = StrategyParser()
        sample = {
            "name": "RSI Mean Reversion",
            "type": "rule",
            "timeframe": "1h",         # required field
            "direction": "long",
            "entry_long": {"conditions": ["rsi_14 < 30"], "logic": "AND"},
            "exit_long":  {"conditions": ["rsi_14 > 60"], "logic": "OR"},
            "risk": {"stop_loss_pct": 2.0, "take_profit_pct": 4.0, "position_size_pct": 10.0}
        }
        r = parser.parse(json.dumps(sample))
        assert r is not None, "parse() returned None"
        assert r.get("name") == "RSI Mean Reversion"
        return f"name={r['name']} type={r['type']}"
    test("StrategyParser: parse() extracts and validates JSON", check_parser_parse)


# ═══════════════════════════════════════════════════════════════
# SECTION 10 — Audit Fix Verification
# ═══════════════════════════════════════════════════════════════
if should_run(10):
    _hdr(10, "Audit Fix Verification (items 1-23)")

    def check_a3_orchestrator_weight():
        from config.settings import settings; settings.set("rl.enabled", False)
        from core.meta_decision.confluence_scorer import ConfluenceScorer
        cs = ConfluenceScorer()
        assert "orchestrator" in cs._weights
        assert cs._weights["orchestrator"] > 0
        return f"orchestrator={cs._weights['orchestrator']}"
    test("Audit #3: Orchestrator as weighted vote in ConfluenceScorer", check_a3_orchestrator_weight)

    def check_a4_slippage():
        from core.execution.paper_executor import PaperExecutor
        pe = PaperExecutor()
        for _ in range(20):
            assert pe._apply_slippage(10000.0, "buy")  > 10000.0
            assert pe._apply_slippage(10000.0, "sell") < 10000.0
        return "slippage always correct direction"
    test("Audit #4: PaperExecutor slippage correct direction", check_a4_slippage)

    def check_a5_rl_weight():
        from config.settings import settings; settings.set("rl.enabled", False)
        from core.meta_decision.confluence_scorer import ConfluenceScorer
        cs = ConfluenceScorer()
        assert cs._weights.get("rl_ensemble", -1) == 0.0
        return "rl_ensemble=0.0 when disabled"
    test("Audit #5: rl_ensemble weight zeroed when RL disabled", check_a5_rl_weight)

    def check_a6_warmup():
        import pandas as pd, numpy as np
        from core.signals.signal_generator import SignalGenerator
        sg = SignalGenerator()
        sg.reset_warmup(100)
        assert sg._warmup_bars_remaining == 100
        idx = pd.date_range("2024-01-01", periods=5, freq="1h")
        df = pd.DataFrame({"open":[42000.0]*5,"high":[43000.0]*5,"low":[41000.0]*5,
                           "close":[42000.0]*5,"volume":[100.0]*5}, index=idx)
        assert sg.generate("BTC/USDT", df, "bull_trend", "1h") == []
        return "warmup=100 bars suppresses signals"
    test("Audit #6: Regime warm-up suppresses signals", check_a6_warmup)

    def check_a11_hysteresis():
        from core.regime.regime_classifier import RegimeClassifier
        rc = RegimeClassifier()
        assert rc._hysteresis_bars == 3
        assert hasattr(rc, "_regime_buffer") and hasattr(rc, "_committed_regime")
        return "3-bar hysteresis buffer present"
    test("Audit #11: RegimeClassifier 3-bar hysteresis", check_a11_hysteresis)

    def check_a12_runner_model():
        from core.strategies.strategy_runner import get_strategy_runner
        from core.signals.sub_models.custom_rule_model import CustomRuleModel
        runner = get_strategy_runner()
        tree = {"type":"AND","children":[{"type":"leaf","left":"close","op":">","right":1}]}
        ok = runner.load_strategy({"name":"Chk","entry_long":tree,
                                   "stop_loss_pct":2.0,"take_profit_pct":4.0,"timeframe":"1h"})
        assert ok and isinstance(runner.model, CustomRuleModel)
        runner.deactivate()
        return "StrategyRunner -> CustomRuleModel wired"
    test("Audit #12: StrategyRunner <-> CustomRuleModel", check_a12_runner_model)

    def check_a21_poll_intervals():
        from core.agents.sector_rotation_agent import _POLL_SECONDS as sec_rot
        from core.agents.geopolitical_agent   import _POLL_SECONDS as geo
        from core.agents.miner_flow_agent     import _POLL_SECONDS as miner
        from core.agents.stablecoin_agent     import _POLL_SECONDS as stable
        assert sec_rot == 14400, f"sector_rotation={sec_rot} (expected 14400)"
        assert geo      == 21600, f"geopolitical={geo} (expected 21600)"
        assert miner    == 43200, f"miner_flow={miner} (expected 43200)"
        assert stable   == 7200,  f"stablecoin={stable} (expected 7200)"
        return "sector=4h geo=6h miner=12h stable=2h"
    test("Audit #21: Low-frequency agent poll intervals", check_a21_poll_intervals)

    def check_a22_social():
        from core.agents.social_sentiment_agent import SocialSentimentAgent
        agent = SocialSentimentAgent()
        raw = {
            "fng":      {"value": 55, "value_classification": "Greed"},
            "twitter":  {"signal": 0.3, "confidence": 0.6},
            "reddit":   {"signal": 0.1, "confidence": 0.4},
            "telegram": {"signal": 0.2, "confidence": 0.5},
        }
        result = agent.process(raw)
        comps = result.get("components", {})
        for src in ("fear_greed", "twitter", "reddit", "telegram"):
            assert src in comps, f"missing component: {src}"
        return f"{len(comps)} sources aggregated"
    test("Audit #22: SocialSentimentAgent consolidates all sources", check_a22_social)

    def check_a23_imports():
        mods = [
            "core.agents.miner_flow_agent",
            "core.agents.onchain_agent",
            "core.agents.squeeze_detection_agent",
            "core.agents.stablecoin_agent",
            "core.agents.whale_agent",
            "core.nlp.news_feed",
        ]
        for m in mods:
            importlib.import_module(m)
        return f"{len(mods)} previously-broken imports OK"
    test("Audit #23: Bug-fixed agent imports load cleanly", check_a23_imports)

    def check_a23_get_router():
        from core.execution.order_router import get_router, order_router
        assert get_router() is order_router
        return "get_router() returns singleton"
    test("Audit #23: get_router() alias present", check_a23_get_router)

    def check_a23_topics():
        from core.event_bus import Topics
        assert hasattr(Topics, "NOTIFICATION_SENT")
        return f"value={Topics.NOTIFICATION_SENT}"
    test("Audit #23: Topics.NOTIFICATION_SENT defined", check_a23_topics)

    def check_news_key_gate():
        from core.sentiment.news_fetcher import _fetch_newsapi
        assert _fetch_newsapi(api_key="short") == []
        assert _fetch_newsapi(api_key="") == []
        assert _fetch_newsapi(api_key=None) == []
        return "invalid keys silently rejected"
    test("Audit: NewsAPI invalid key silently gated", check_news_key_gate)


# ═══════════════════════════════════════════════════════════════
# SECTION 11 — Strategies Module Redesign
# ═══════════════════════════════════════════════════════════════
if should_run(11):
    _hdr(11, "Strategies Module Redesign (audit 9.2-9.4)")

    def check_strategy_classes():
        import ast
        src = (ROOT / "gui/pages/strategies/strategies_page.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        classes = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
        for cls in ("StrategiesPage", "StrategyLibraryTab", "LiveRunnerTab",
                    "LifecycleStepper", "StrategyEditorPanel"):
            assert cls in classes, f"class {cls} missing"
        return f"{len(classes)} classes: {', '.join(sorted(classes))}"
    test("StrategiesPage: all redesigned classes present", check_strategy_classes)

    def check_type_taxonomy():
        src = (ROOT / "gui/pages/strategies/strategies_page.py").read_text(encoding="utf-8")
        for t in ("rule", "ai", "ml", "ensemble"):
            assert f'"{t}"' in src, f"type '{t}' missing"
        return "all 4 types: rule / ai / ml / ensemble"
    test("StrategiesPage: Strategy Type Taxonomy", check_type_taxonomy)

    def check_lifecycle_stages():
        src = (ROOT / "gui/pages/strategies/strategies_page.py").read_text(encoding="utf-8")
        for stage in ("Generation", "Backtesting", "Walk-Forward",
                      "Out-of-Sample", "Shadow Trading", "Live Trading"):
            assert stage in src, f"stage '{stage}' missing"
        return "all 6 lifecycle stages present"
    test("StrategiesPage: all 6 lifecycle stages", check_lifecycle_stages)

    def check_page_base():
        import ast
        src = (ROOT / "gui/page_base.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        classes = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
        for cls in ("PageBase", "PageStatusBar", "_Worker"):
            assert cls in classes, f"{cls} missing from page_base.py"
        return f"classes: {', '.join(sorted(classes))}"
    test("PageBase: UI architecture base class", check_page_base)

    def check_exec_paper_btn():
        src = (ROOT / "gui/pages/market_scanner/scanner_page.py").read_text(encoding="utf-8")
        assert "_execute_to_paper" in src, "_execute_to_paper method missing"
        assert "Execute to Paper" in src,  "'Execute to Paper' label missing"
        assert "order_router.submit" in src, "order_router.submit call missing"
        return "button + routing present"
    test("ScannerPage: Execute to Paper Trading button", check_exec_paper_btn)


# ═══════════════════════════════════════════════════════════════
# SUMMARY
# ═══════════════════════════════════════════════════════════════
total = PASS_COUNT + FAIL_COUNT + SKIP_COUNT
print(f"\n{BLD}{'='*60}{RST}")
print(f"{BLD}  RESULTS{RST}")
print(f"{'='*60}")
print(f"  {GRN}PASSED {RST}: {PASS_COUNT}")
print(f"  {RED}FAILED {RST}: {FAIL_COUNT}")
print(f"  {YLW}SKIPPED{RST}: {SKIP_COUNT}")
print(f"  TOTAL  : {total}")

if FAILS:
    print(f"\n{BLD}{RED}  FAILURES:{RST}")
    for name, reason in FAILS:
        short = reason.splitlines()[-1][:100] if reason else "unknown"
        print(f"  {RED}x{RST} {name}")
        print(f"    {DIM}{short}{RST}")

print(f"\n{'='*60}")
if FAIL_COUNT == 0:
    print(f"{BLD}{GRN}  All tests passed! {RST}")
else:
    print(f"{BLD}{RED}  {FAIL_COUNT} test(s) failed.{RST}")
    print(f"  Rerun with {YLW}--verbose{RST} for full tracebacks.")
print(f"{'='*60}\n")

sys.exit(0 if FAIL_COUNT == 0 else 1)
