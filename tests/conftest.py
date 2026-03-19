"""
NexusTrader — pytest session fixtures (tests/conftest.py)

IMPORTANT — headless / CI display setup
Qt requires a display backend.  In headless environments (Linux CI, VMs
without X11/Wayland/EGL) the offscreen platform is used so that QApplication
and all QObject subclasses (including EventBus) can be instantiated without
a real display.  The env var must be set *before* any PySide6 import occurs
(Qt reads it at load time), so it is set here at module level.
"""
from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest


# ═══════════════════════════════════════════════════════════════════
#  PaperExecutor disk-I/O isolation  (autouse — every test)
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _isolate_paper_executor_disk_io(monkeypatch):
    """
    Prevent ALL PaperExecutor instances from touching the real
    data/open_positions.json during any test.

    Without this, tests that create PaperExecutor() directly (rather than
    via the `paper_executor` fixture) could:
      1. Read stale positions from a previous test run and inherit
         incorrect capital / open positions.
      2. Write test positions to the live data file, corrupting the
         state that NexusTrader uses when it starts up.

    Patching at the class level covers both the `paper_executor` fixture
    instances AND any PaperExecutor() created inline inside a test body.
    """
    try:
        from core.execution.paper_executor import PaperExecutor
        monkeypatch.setattr(PaperExecutor, "_load_open_positions", lambda self: None)
        monkeypatch.setattr(PaperExecutor, "_save_open_positions", lambda self: None)
    except Exception:
        pass  # If import fails for unrelated reasons, don't block the test


# ═══════════════════════════════════════════════════════════════════
#  QApplication  (session — must live for entire test run)
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture(scope="session")
def qt_app():
    """
    Creates (or reuses) the QApplication singleton.

    Qt panics if you create a second QApplication in the same process, so we
    check QApplication.instance() first.  The fixture is session-scoped so Qt
    is initialised once regardless of how many tests request it.
    """
    # Use QCoreApplication (no display/EGL required) for headless test environments.
    # Full QApplication is only needed for tests that render widgets; none of our
    # current unit tests do, so QCoreApplication is sufficient.
    from PySide6.QtCore import QCoreApplication

    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication(sys.argv[:1])

    yield app
    # Do NOT call app.quit() here — other session fixtures may still need Qt.


# ═══════════════════════════════════════════════════════════════════
#  In-memory SQLite database
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture
def test_db(monkeypatch):
    """
    Per-test isolated SQLite database running entirely in memory.

    Patches three globals in core.database.engine so that any module that
    does `from core.database.engine import get_session / engine / SessionLocal`
    transparently uses the in-memory engine for the duration of the test.

    Yields the SQLAlchemy engine so tests can inspect tables directly.
    """
    from sqlalchemy import create_engine, event as sa_event
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import core.database.engine as db_module
    import core.database.models  # noqa: F401 — registers all ORM models against Base

    from core.database.engine import Base

    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )

    @sa_event.listens_for(test_engine, "connect")
    def _set_pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    # Create every table defined in models.py
    Base.metadata.create_all(test_engine)

    TestSession = sessionmaker(bind=test_engine, autocommit=False, autoflush=False)

    @contextmanager
    def _get_session():
        session = TestSession()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(db_module, "engine",       test_engine)
    monkeypatch.setattr(db_module, "SessionLocal", TestSession)
    monkeypatch.setattr(db_module, "get_session",  _get_session)

    yield test_engine

    test_engine.dispose()


# ═══════════════════════════════════════════════════════════════════
#  EventBus  (isolated per test)
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture
def event_bus(qt_app):
    """
    A fresh EventBus instance for each test.

    This is *not* the global `bus` singleton.  Tests that exercise components
    wired to the global bus should monkeypatch `core.event_bus.bus` themselves,
    or use the `event_capture` helper fixture defined below.
    """
    from core.event_bus import EventBus

    eb = EventBus()
    yield eb
    eb.clear_subscribers()


# ═══════════════════════════════════════════════════════════════════
#  Mock CCXT exchange
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture
def mock_exchange():
    """
    MagicMock that looks like a live CCXT exchange instance.

    Pre-configured with realistic return values for the methods used by
    NexusTrader components.  Individual tests can override any method:

        mock_exchange.fetch_ticker.return_value = {...}
        mock_exchange.create_order.side_effect = ccxt.NetworkError("timeout")
    """
    now_ms = int(datetime.utcnow().timestamp() * 1000)
    interval_ms = 3_600_000  # 1 hour in milliseconds

    ex = MagicMock()
    ex.id = "kucoin"
    ex.name = "KuCoin"
    ex.has = {
        "fetchTicker": True,
        "fetchOHLCV": True,
        "createOrder": True,
        "fetchPositions": True,
        "cancelOrder": True,
        "fetchOrderBook": True,
        "fetchFundingRate": True,
    }

    # ── fetch_ticker ──────────────────────────────────────────
    ex.fetch_ticker.return_value = {
        "symbol":     "BTC/USDT",
        "last":       65_000.0,
        "bid":        64_990.0,
        "ask":        65_010.0,
        "high":       66_000.0,
        "low":        64_000.0,
        "open":       64_500.0,
        "close":      65_000.0,
        "volume":     12_345.0,
        "percentage": 0.77,
        "change":     500.0,
        "timestamp":  now_ms,
        "datetime":   datetime.utcnow().isoformat(),
        "info":       {},
    }

    # ── fetch_ohlcv ───────────────────────────────────────────
    # 100 flat bars — override per test for scenario-specific data
    ex.fetch_ohlcv.return_value = [
        [now_ms - (100 - i) * interval_ms,
         65_000.0, 65_100.0, 64_900.0, 65_000.0, 100.0]
        for i in range(100)
    ]

    # ── create_order ──────────────────────────────────────────
    ex.create_order.return_value = {
        "id":        "test_order_001",
        "symbol":    "BTC/USDT",
        "type":      "market",
        "side":      "buy",
        "amount":    0.001,
        "price":     65_000.0,
        "status":    "closed",
        "filled":    0.001,
        "cost":      65.0,
        "timestamp": now_ms,
        "info":      {},
    }

    # ── fetch_positions / cancel_order / fetch_order_book ─────
    ex.fetch_positions.return_value = []
    ex.cancel_order.return_value   = {"id": "test_order_001", "status": "canceled"}
    ex.fetch_order_book.return_value = {
        "bids":      [[64_990.0, 1.0], [64_980.0, 2.0], [64_970.0, 0.5]],
        "asks":      [[65_010.0, 1.0], [65_020.0, 2.0], [65_030.0, 0.5]],
        "timestamp": now_ms,
        "datetime":  datetime.utcnow().isoformat(),
    }

    # ── fetch_funding_rate ────────────────────────────────────
    ex.fetch_funding_rate.return_value = {
        "symbol":      "BTC/USDT",
        "fundingRate": 0.0001,
        "timestamp":   now_ms,
        "info":        {},
    }

    return ex


# ═══════════════════════════════════════════════════════════════════
#  PaperExecutor  (backed by isolated DB)
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture
def paper_executor(qt_app, test_db):
    """
    PaperExecutor with 10,000 USDT initial capital backed by the in-memory
    test database.

    The `test_db` fixture must be listed first so core.database.engine is
    patched *before* PaperExecutor.__init__ calls _load_history().

    IMPORTANT: _save_open_positions is patched to a no-op so that tests
    never write to the real data/open_positions.json file on disk.
    """
    from unittest.mock import patch
    from core.execution import paper_executor as _pe_module
    from core.execution.paper_executor import PaperExecutor

    pe = PaperExecutor(initial_capital_usdt=10_000.0)
    # Ensure test isolation: clear any positions loaded from persistent file
    # (open_positions.json may contain real demo-trading state)
    pe._positions.clear()
    pe._capital = 10_000.0

    # Patch _save_open_positions to a no-op so tests cannot corrupt the
    # real data/open_positions.json that the live app relies on.
    pe._save_open_positions = lambda: None

    yield pe

    # Teardown: unsubscribe event handler to prevent cross-test bleed
    try:
        from core.event_bus import bus, Topics
        bus.unsubscribe(Topics.POSITION_MONITOR_UPDATED, pe._on_position_monitor)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════
#  Mock KeyVault  (isolated to tmp_path)
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture
def mock_keyvault(tmp_path, monkeypatch):
    """
    KeyVault instance isolated to a pytest tmp_path directory.

    Pre-seeded with harmless dummy API keys so any component that calls
    key_vault.load('...') receives a non-empty test value.

    Also patches the global singleton in core.security.key_vault so that
    importing `key_vault` from that module gets this test instance.
    """
    from core.security.key_vault import KeyVault
    import core.security.key_vault as kv_module

    vault = KeyVault()
    vault._vault_path = tmp_path / ".nexus_vault.json"
    vault._key_path   = tmp_path / ".nexus_key"

    # Seed dummy credentials
    vault.save("ai.anthropic_api_key",           "TEST_ANTHROPIC_KEY_abc123")
    vault.save("ai.openai_api_key",              "TEST_OPENAI_KEY_abc123")
    vault.save("sentiment.news_api_key",         "TEST_NEWS_KEY_abc123")
    vault.save("sentiment.reddit_client_id",     "TEST_REDDIT_ID_abc123")
    vault.save("sentiment.reddit_client_secret", "TEST_REDDIT_SECRET_abc123")

    monkeypatch.setattr(kv_module, "key_vault", vault)

    yield vault


# ═══════════════════════════════════════════════════════════════════
#  Synthetic OHLCV factory
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture
def synthetic_ohlcv():
    """
    Factory that returns deterministic OHLCV DataFrames for named scenarios.

    Usage
    -----
    def test_something(synthetic_ohlcv):
        df = synthetic_ohlcv("trend", n=300)
        df = synthetic_ohlcv("crash", n=150, base_price=65_000)

    Scenarios
    ---------
    "trend"           — Persistent uptrend (0.05% per bar) with small noise.
    "downtrend"       — Persistent downtrend (−0.05% per bar) with small noise.
    "mean_reversion"  — Sine-wave oscillation ±2% around base_price.
    "crash"           — Stable then flash-crash −12% over 3 bars.
    "sideways"        — Very low-volatility range (±0.15%).
    "breakout"        — Flat consolidation then explosive +8% move over 5 bars.
    """
    def _make(
        scenario:   str   = "trend",
        n:          int   = 200,
        base_price: float = 65_000.0,
        seed:       int   = 42,          # keep deterministic; override to vary
    ) -> pd.DataFrame:
        rng = np.random.default_rng(seed=seed)
        now = datetime.utcnow()
        timestamps = [now - timedelta(hours=(n - i)) for i in range(n)]

        if scenario == "trend":
            drift  = np.cumsum(np.ones(n) * base_price * 0.0005)
            noise  = rng.normal(0, base_price * 0.001, n)
            prices = base_price + drift + noise

        elif scenario == "downtrend":
            drift  = -np.cumsum(np.ones(n) * base_price * 0.0005)
            noise  = rng.normal(0, base_price * 0.001, n)
            prices = base_price + drift + noise

        elif scenario == "mean_reversion":
            cycle  = np.sin(np.linspace(0, 4 * np.pi, n)) * base_price * 0.02
            noise  = rng.normal(0, base_price * 0.003, n)
            prices = base_price + cycle + noise

        elif scenario == "crash":
            noise  = rng.normal(0, base_price * 0.001, n)
            prices = base_price + noise
            # Flash crash: −4% per bar for 3 bars starting at 80% mark
            crash_start = int(n * 0.80)
            for j in range(3):
                idx = crash_start + j
                if idx < n:
                    prices[idx] = prices[crash_start - 1] * (1.0 - 0.04 * (j + 1))

        elif scenario == "sideways":
            noise  = rng.normal(0, base_price * 0.0015, n)
            prices = base_price + noise

        elif scenario == "breakout":
            noise  = rng.normal(0, base_price * 0.001, n)
            prices = base_price + noise
            # Explosive breakout: +1.6% per bar for 5 bars at 75% mark
            bo_start = int(n * 0.75)
            for j in range(5):
                idx = bo_start + j
                if idx < n:
                    prices[idx] = prices[bo_start - 1] * (1.0 + 0.016 * (j + 1))

        else:
            raise ValueError(
                f"Unknown scenario {scenario!r}. "
                "Choose: trend, downtrend, mean_reversion, crash, sideways, breakout"
            )

        prices = np.clip(prices, 1.0, None)  # no zero/negative prices

        # Build OHLCV from close prices
        opens  = np.roll(prices, 1); opens[0] = prices[0]
        highs  = np.maximum(opens, prices) * (1 + rng.uniform(0.0, 0.002, n))
        lows   = np.minimum(opens, prices) * (1 - rng.uniform(0.0, 0.002, n))
        vols   = rng.uniform(50.0, 500.0, n) * (base_price / 65_000.0)

        return pd.DataFrame({
            "timestamp": timestamps,
            "open":      opens.astype(float),
            "high":      highs.astype(float),
            "low":       lows.astype(float),
            "close":     prices.astype(float),
            "volume":    vols.astype(float),
        })

    return _make


# ═══════════════════════════════════════════════════════════════════
#  OrderCandidate factory
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture
def make_candidate():
    """
    Factory fixture that builds valid OrderCandidate objects.

    Usage
    -----
    def test_something(make_candidate):
        c = make_candidate()                             # BUY BTC/USDT @65000, score=0.78
        c = make_candidate(side="sell", score=0.92)
        c = make_candidate(symbol="ETH/USDT", entry_price=3_000.0,
                           stop_loss=2_940.0, take_profit=3_120.0)

    All arguments are optional — sensible defaults are provided for every field.
    """
    from core.meta_decision.order_candidate import OrderCandidate

    def _factory(
        symbol:          str   = "BTC/USDT",
        side:            str   = "buy",          # "buy" | "sell"
        entry_price:     float = 65_000.0,
        stop_loss:       float = 63_700.0,       # ~2 % below entry  (1:2 R:R default)
        take_profit:     float = 67_600.0,       # ~4 % above entry
        size_usdt:       float = 100.0,
        score:           float = 0.78,
        models_fired:    list  = None,
        regime:          str   = "TRENDING_UP",
        timeframe:       str   = "1h",
        atr_value:       float = 650.0,
        rationale:       str   = "Test candidate — confluence threshold met",
        expiry_seconds:  int   = 3_600,
        approved:        bool  = False,
    ) -> OrderCandidate:
        c = OrderCandidate(
            symbol             = symbol,
            side               = side,
            entry_type         = "limit",
            entry_price        = entry_price,
            stop_loss_price    = stop_loss,
            take_profit_price  = take_profit,
            position_size_usdt = size_usdt,
            score              = score,
            models_fired       = models_fired or ["trend", "momentum_breakout"],
            regime             = regime,
            rationale          = rationale,
            timeframe          = timeframe,
            atr_value          = atr_value,
            expiry             = datetime.utcnow() + timedelta(seconds=expiry_seconds),
        )
        c.approved = approved
        return c

    return _factory


# ═══════════════════════════════════════════════════════════════════
#  EventBus event capture helper
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture
def event_capture(qt_app):
    """
    Captures events published on the *global* event bus during a test.

    Usage
    -----
    def test_pipeline(event_capture):
        capture = event_capture(Topics.TRADE_OPENED, Topics.TRADE_CLOSED)
        # ... trigger the pipeline ...
        assert len(capture[Topics.TRADE_OPENED]) == 1
        assert capture[Topics.TRADE_OPENED][0].data["symbol"] == "BTC/USDT"

    The returned capture dict maps topic → list[Event].
    All subscriptions are removed in teardown.
    """
    from core.event_bus import bus

    registered: list[tuple[str, object]] = []

    def _subscribe(*topics: str) -> dict:
        captured: dict[str, list] = {t: [] for t in topics}

        for topic in topics:
            def _handler(event, t=topic):
                captured[t].append(event)

            bus.subscribe(topic, _handler)
            registered.append((topic, _handler))

        return captured

    yield _subscribe

    # Teardown: remove all handlers registered during this test
    for topic, handler in registered:
        bus.unsubscribe(topic, handler)


# ═══════════════════════════════════════════════════════════════════
#  GPU suppressor
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture
def no_gpu(monkeypatch):
    """
    Forces CPU-only mode for the duration of the test by patching
    torch.cuda.is_available to return False.

    Silently skips patching if torch is not installed.
    """
    try:
        import torch
        monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    except ImportError:
        pass
    yield


# ═══════════════════════════════════════════════════════════════════
#  Marker registration  (suppresses PytestUnknownMarkWarning)
# ═══════════════════════════════════════════════════════════════════

def pytest_configure(config):
    markers = [
        ("unit",        "Pure unit test — no external dependencies"),
        ("integration", "Uses EventBus or DB"),
        ("system",      "Full pipeline with mock exchange"),
        ("gpu",         "Requires CUDA-capable GPU (RTX 4070)"),
        ("exchange",    "Calls real Bybit Demo API — manual execution only"),
        ("slow",        "Runtime > 30 seconds — nightly CI only"),
        ("ui",          "Requires visible Qt window — manual execution only"),
    ]
    for name, description in markers:
        config.addinivalue_line("markers", f"{name}: {description}")


def pytest_addoption(parser):
    """Register custom CLI flags."""
    parser.addoption(
        "--include-slow",
        action="store_true",
        default=False,
        help=(
            "Include @pytest.mark.slow tests (runtime > 30 s). "
            "Omit this flag for normal runs; add it in nightly CI only. "
            "Example:  pytest --include-slow tests/intelligence/"
        ),
    )


# ═══════════════════════════════════════════════════════════════════
#  Auto-skip gpu / exchange / ui / slow markers in headless CI
# ═══════════════════════════════════════════════════════════════════

def pytest_runtest_setup(item):
    """Auto-skip tests that need GPU, live exchange, display, or slow flag."""

    if item.get_closest_marker("gpu"):
        try:
            import torch
            if not torch.cuda.is_available():
                pytest.skip("GPU not available (torch.cuda.is_available() is False)")
        except ImportError:
            pytest.skip("torch not installed — GPU tests skipped")

    if item.get_closest_marker("exchange"):
        pytest.skip(
            "Exchange tests require a live Bybit Demo account — "
            "run manually with: pytest -m exchange"
        )

    if item.get_closest_marker("ui"):
        pytest.skip(
            "UI tests require a visible Qt window — "
            "run manually with: pytest -m ui"
        )

    if item.get_closest_marker("slow"):
        if not item.config.getoption("--include-slow", default=False):
            pytest.skip(
                "Slow test skipped in standard runs. "
                "Use --include-slow to enable (nightly CI only)."
            )
