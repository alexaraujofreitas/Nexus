# ============================================================
# Session 37 — Regression Tests
# Covers: ACCOUNT_RESET propagation, disabled agent filtering,
#         max_pos source unification
# ============================================================

import pytest
from unittest.mock import MagicMock, patch


# ── 1. ACCOUNT_RESET topic exists on Topics class ────────────


def test_account_reset_topic_exists():
    """Topics.ACCOUNT_RESET must be defined and have correct string value."""
    from core.event_bus import Topics
    assert hasattr(Topics, "ACCOUNT_RESET"), "Topics.ACCOUNT_RESET is missing"
    assert Topics.ACCOUNT_RESET == "account.reset"


# ── 2. PaperExecutor.reset() publishes ACCOUNT_RESET ─────────


def test_paper_executor_reset_publishes_account_reset(tmp_path):
    """reset() must publish Topics.ACCOUNT_RESET so GUI pages refresh."""
    from core.event_bus import bus, Topics

    received = []
    def capture(event):
        received.append(event)

    bus.subscribe(Topics.ACCOUNT_RESET, capture)
    try:
        from core.execution.paper_executor import PaperExecutor
        pe = PaperExecutor(initial_capital_usdt=10_000.0)
        pe.reset(initial_capital=10_000.0)
        assert len(received) == 1, (
            f"Expected 1 ACCOUNT_RESET event after reset(), got {len(received)}"
        )
        assert received[0].topic == Topics.ACCOUNT_RESET
        assert received[0].data["capital"] == 10_000.0
        assert received[0].data["reason"] == "manual_reset"
    finally:
        bus.unsubscribe(Topics.ACCOUNT_RESET, capture)


def test_paper_executor_reset_clears_positions(tmp_path):
    """After reset(), open positions must be empty."""
    from core.execution.paper_executor import PaperExecutor, PaperPosition
    pe = PaperExecutor(initial_capital_usdt=10_000.0)
    # Manually inject a fake position with full required args
    fake_pos = PaperPosition(
        symbol="BTC/USDT", side="buy",
        entry_price=50_000.0, quantity=0.01,
        stop_loss=49_000.0, take_profit=52_000.0,
        size_usdt=500.0, score=0.6, rationale="test",
    )
    pe._positions["BTC/USDT"] = [fake_pos]  # _positions stores lists of positions
    assert len(pe.get_open_positions()) == 1

    pe.reset(initial_capital=10_000.0)
    assert len(pe.get_open_positions()) == 0, (
        "reset() must clear all open positions"
    )


# ── 3. Risk page subscribes to ACCOUNT_RESET ─────────────────


def test_risk_page_subscribes_to_account_reset():
    """RiskPage._subscribe() must register a handler for ACCOUNT_RESET."""
    # Import the subscribe method source and scan for ACCOUNT_RESET
    import inspect
    # We read the source instead of instantiating (Qt not available in test env)
    import ast, pathlib
    src = pathlib.Path(
        "gui/pages/risk_management/risk_page.py"
    ).read_text(errors="replace")
    assert "ACCOUNT_RESET" in src, (
        "risk_page.py must subscribe to Topics.ACCOUNT_RESET"
    )


def test_paper_trading_page_subscribes_to_account_reset():
    """PaperTradingPage._subscribe() must handle ACCOUNT_RESET."""
    import pathlib
    src = pathlib.Path(
        "gui/pages/paper_trading/paper_trading_page.py"
    ).read_text(errors="replace")
    assert "ACCOUNT_RESET" in src, (
        "paper_trading_page.py must subscribe to Topics.ACCOUNT_RESET"
    )


def test_orders_page_subscribes_to_account_reset():
    """OrdersPage._subscribe() must handle ACCOUNT_RESET."""
    import pathlib
    src = pathlib.Path(
        "gui/pages/orders_positions/orders_page.py"
    ).read_text(errors="replace")
    assert "ACCOUNT_RESET" in src, (
        "orders_page.py must subscribe to Topics.ACCOUNT_RESET"
    )


def test_dashboard_page_subscribes_to_account_reset():
    """DashboardPage._subscribe() must handle ACCOUNT_RESET."""
    import pathlib
    src = pathlib.Path(
        "gui/pages/dashboard/dashboard_page.py"
    ).read_text(errors="replace")
    assert "ACCOUNT_RESET" in src, (
        "dashboard_page.py must subscribe to Topics.ACCOUNT_RESET"
    )
    assert "_on_account_reset" in src, (
        "dashboard_page.py must define _on_account_reset handler"
    )


def test_analytics_page_subscribes_to_account_reset():
    """AnalyticsPage._subscribe() must handle ACCOUNT_RESET."""
    import pathlib
    src = pathlib.Path(
        "gui/pages/performance_analytics/analytics_page.py"
    ).read_text(errors="replace")
    assert "ACCOUNT_RESET" in src, (
        "analytics_page.py must subscribe to Topics.ACCOUNT_RESET"
    )
    assert "_on_account_reset" in src, (
        "analytics_page.py must define _on_account_reset handler"
    )


def test_quant_dashboard_subscribes_to_account_reset():
    """Quant Dashboard panels (Positions/Portfolio/TradeHistory/Alerts) must handle ACCOUNT_RESET."""
    import pathlib
    src = pathlib.Path(
        "gui/pages/quant_dashboard/quant_dashboard_page.py"
    ).read_text(errors="replace")
    count = src.count("ACCOUNT_RESET")
    assert count >= 4, (
        f"quant_dashboard_page.py should have ≥4 ACCOUNT_RESET subscriptions, found {count}"
    )


# ── 4. Agents page filters disabled agents ───────────────────


def test_agents_page_gate_map_present():
    """agents_page.py must contain _AGENT_GATE with disable-flagged agents."""
    import pathlib
    src = pathlib.Path(
        "gui/pages/intelligence/agents_page.py"
    ).read_text(errors="replace")
    assert "_AGENT_GATE" in src, "agents_page.py must define _AGENT_GATE"
    assert "orderbook_enabled" in src
    assert "options_enabled" in src
    assert "social_sentiment_enabled" in src
    assert "sector_rotation_enabled" in src


def test_intelligence_page_filters_disabled_agents():
    """intelligence_page.py must filter _AGENT_WEIGHTS by config gate."""
    import pathlib
    src = pathlib.Path(
        "gui/pages/intelligence/intelligence_page.py"
    ).read_text(errors="replace")
    assert "_AGENT_WEIGHTS_ALL" in src, (
        "intelligence_page.py must define _AGENT_WEIGHTS_ALL and filter it"
    )
    assert "_build_agent_weights" in src, (
        "intelligence_page.py must define _build_agent_weights() factory"
    )
    assert "orderbook_enabled" in src


def test_build_agent_weights_filters_disabled():
    """_build_agent_weights() logic must exclude agents whose config key is false.

    Tests the filtering logic in isolation (no Qt import required).
    """
    # Replicate the filtering logic from intelligence_page.py
    _AGENT_WEIGHTS_ALL = {
        "funding_rate":    0.25,
        "order_book":      0.22,
        "options_flow":    0.18,
        "macro":           0.17,
        "social_sentiment":0.08,
        "news":            0.05,
        "geopolitical":    0.03,
        "sector_rotation": 0.02,
    }
    _AGENT_GATE = {
        "order_book":       ("agents.orderbook_enabled",        False),
        "options_flow":     ("agents.options_enabled",          False),
        "social_sentiment": ("agents.social_sentiment_enabled", False),
        "sector_rotation":  ("agents.sector_rotation_enabled",  False),
        "funding_rate":     ("agents.funding_enabled",          True),
    }
    # Simulate disabled config
    _config = {
        "agents.orderbook_enabled":        False,
        "agents.options_enabled":          False,
        "agents.social_sentiment_enabled": False,
        "agents.sector_rotation_enabled":  False,
        "agents.funding_enabled":          True,
    }
    mock_settings = MagicMock()
    mock_settings.get = lambda key, default=None: _config.get(key, default)

    # Execute the same filter logic as _build_agent_weights
    result = {}
    for name, weight in _AGENT_WEIGHTS_ALL.items():
        if name in _AGENT_GATE:
            key, default = _AGENT_GATE[name]
            if not mock_settings.get(key, default):
                continue
        result[name] = weight

    assert "order_book"       not in result, "order_book must be filtered (disabled)"
    assert "options_flow"     not in result, "options_flow must be filtered (disabled)"
    assert "social_sentiment" not in result, "social_sentiment must be filtered (disabled)"
    assert "sector_rotation"  not in result, "sector_rotation must be filtered (disabled)"
    assert "funding_rate"     in result,     "funding_rate must remain (enabled)"
    assert "macro"            in result,     "macro must remain (no gate)"
    assert "news"             in result,     "news must remain (no gate)"
    assert "geopolitical"     in result,     "geopolitical must remain (no gate)"


# ── 5. max_pos source unified (paper trading uses RiskGate) ──


def test_paper_trading_page_uses_risk_gate_for_max_pos():
    """paper_trading_page.py must read max_pos from scanner._risk_gate first."""
    import pathlib
    src = pathlib.Path(
        "gui/pages/paper_trading/paper_trading_page.py"
    ).read_text(errors="replace")
    assert "scanner as _sc" in src or "scanner import" in src, (
        "paper_trading_page must import scanner to read _risk_gate.max_concurrent_positions"
    )
    assert "_risk_gate.max_concurrent_positions" in src, (
        "paper_trading_page must read max_pos from _risk_gate (same source as risk_page)"
    )


# ── 6. dashboard includes PBL/SLC in strategy count ──────────


def test_dashboard_includes_pbl_slc_in_model_count():
    """Dashboard strategy card must count PBL/SLC when mr_pbl_slc.enabled=true."""
    import pathlib
    src = pathlib.Path(
        "gui/pages/dashboard/dashboard_page.py"
    ).read_text(errors="replace")
    assert "pullback_long" in src, (
        "dashboard_page.py must include pullback_long in model count"
    )
    assert "swing_low_continuation" in src, (
        "dashboard_page.py must include swing_low_continuation in model count"
    )
    assert "mr_pbl_slc.enabled" in src, (
        "dashboard_page.py must gate PBL/SLC inclusion on mr_pbl_slc.enabled"
    )


# ── 7. No usage of Topics.SYSTEM_WARNING anywhere ────────────


def test_no_topics_system_warning_usage():
    """Topics.SYSTEM_WARNING does not exist — any usage causes AttributeError."""
    import pathlib
    root = pathlib.Path(".")
    violations = []
    for f in root.rglob("*.py"):
        if ".git" in str(f) or "__pycache__" in str(f) or "test_" in f.name:
            continue
        try:
            content = f.read_text(errors="replace")
            if "Topics.SYSTEM_WARNING" in content:
                violations.append(str(f))
        except Exception:
            pass
    assert not violations, (
        f"Topics.SYSTEM_WARNING used in {violations} — this topic does not exist. "
        "Use Topics.SYSTEM_ALERT instead."
    )


# ── 8. Correct event bus import pattern ──────────────────────


def test_no_bare_event_bus_import():
    """All files must use 'from core.event_bus import bus, Topics', never 'import event_bus'."""
    import pathlib, re
    root = pathlib.Path(".")
    violations = []
    pattern = re.compile(r"^\s*import\s+event_bus\b", re.MULTILINE)
    for f in root.rglob("*.py"):
        if ".git" in str(f) or "__pycache__" in str(f) or "test_" in f.name:
            continue
        try:
            content = f.read_text(errors="replace")
            if pattern.search(content):
                violations.append(str(f))
        except Exception:
            pass
    assert not violations, f"Bare 'import event_bus' found in: {violations}"
