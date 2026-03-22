"""
tests/unit/test_coinglass_agent.py
-----------------------------------
Tests for CoinglassAgent and its integration with oi_signal.assess_oi_data_quality().

CG-01  Module imports without error; singleton is non-None
CG-02  assess_oi_data_quality returns (0, "agent_import_error") is GONE
         — now returns (1, "no_data") when agent present but no data
CG-03  get_oi_data returns None gracefully when API key absent
CG-04  get_oi_data returns correct dict shape on successful fetch (mocked)
CG-05  Cache TTL: second call within TTL returns cached result, no extra HTTP
CG-06  Cache miss after TTL expiry triggers new fetch
CG-07  oi_change_1h_pct computed correctly from 2-point history
CG-08  oi_change_1h_pct = 0.0 when only one history point (no prior)
CG-09  _parse_oi handles well-formed response
CG-10  _parse_oi returns None on API error code
CG-11  _parse_oi returns None on empty data list
CG-12  Network timeout → returns None, no exception propagated
CG-13  HTTP 401 → returns None gracefully
CG-14  assess_oi_data_quality returns quality=3 for fresh data (mocked)
CG-15  assess_oi_data_quality returns quality=2 for stale data
"""
import threading
import time
from collections import deque
from unittest.mock import MagicMock, patch

import pytest


# ── helpers ────────────────────────────────────────────────────────────────

def _make_fresh_agent():
    """Return a new CoinglassAgent instance (bypasses module singleton)."""
    from core.agents.coinglass_agent import CoinglassAgent
    return CoinglassAgent()


def _mock_response(oi_per_exchange=None, code="0"):
    resp = MagicMock()
    resp.status_code = 200
    data = oi_per_exchange or [
        {"exchangeName": "Binance", "openInterestAmount": 1_000_000_000.0},
        {"exchangeName": "OKX",     "openInterestAmount":   500_000_000.0},
    ]
    resp.json.return_value = {"code": code, "data": data}
    resp.raise_for_status = MagicMock()
    return resp


# ── CG-01 ─────────────────────────────────────────────────────────────────

def test_cg01_module_import():
    from core.agents.coinglass_agent import coinglass_agent, CoinglassAgent
    assert coinglass_agent is not None
    assert isinstance(coinglass_agent, CoinglassAgent)
    assert hasattr(coinglass_agent, "get_oi_data")


# ── CG-02 ─────────────────────────────────────────────────────────────────

def test_cg02_no_more_agent_import_error():
    """assess_oi_data_quality must NOT return (0, 'agent_import_error')."""
    from core.signals.oi_signal import assess_oi_data_quality
    quality, reason = assess_oi_data_quality("BTC/USDT")
    assert reason != "agent_import_error", (
        f"Still getting agent_import_error — coinglass_agent import broken. Got: {reason}"
    )
    assert quality >= 1   # at minimum: agent present


# ── CG-03 ─────────────────────────────────────────────────────────────────

def test_cg03_returns_none_without_key():
    agent = _make_fresh_agent()
    with patch.object(agent, "_load_api_key", return_value=""):
        result = agent.get_oi_data("BTC/USDT")
    assert result is None


# ── CG-04 ─────────────────────────────────────────────────────────────────

def test_cg04_returns_correct_dict_shape():
    agent = _make_fresh_agent()
    with patch.object(agent, "_load_api_key", return_value="FAKE_KEY"), \
         patch("requests.get", return_value=_mock_response()):
        result = agent.get_oi_data("BTC/USDT")

    assert result is not None
    assert "oi_change_1h_pct" in result
    assert "age_seconds" in result
    assert "raw_oi_usd" in result
    assert "source" in result
    assert result["raw_oi_usd"] == pytest.approx(1_500_000_000.0, rel=1e-3)
    assert result["source"] == "coinglass"


# ── CG-05 ─────────────────────────────────────────────────────────────────

def test_cg05_cache_ttl_no_extra_http():
    """Second call within TTL must not hit the network."""
    agent = _make_fresh_agent()
    now = time.time()

    # Pre-populate cache so first call is a hit
    with agent._lock:
        agent._cache["ETH/USDT"] = {"oi_usd": 500_000_000.0, "ts": now}
        agent._history.setdefault("ETH/USDT", deque(maxlen=12)).append(
            (now, 500_000_000.0)
        )

    with patch("core.agents.coinglass_agent.requests.get") as mock_get:
        result = agent.get_oi_data("ETH/USDT")

    assert mock_get.call_count == 0, "Should serve from cache without HTTP call"
    assert result is not None
    assert result["source"] == "cached"


# ── CG-06 ─────────────────────────────────────────────────────────────────

def test_cg06_cache_miss_after_expiry():
    """Expired cache entry must trigger a new HTTP fetch."""
    agent = _make_fresh_agent()

    # Seed an already-expired cache entry
    with agent._lock:
        agent._cache["SOL/USDT"] = {
            "oi_usd": 400_000_000.0,
            "ts": time.time() - 400,   # 400 s ago > 300 s TTL
        }

    with patch.object(agent, "_load_api_key", return_value="FAKE_KEY"), \
         patch("core.agents.coinglass_agent.requests.get",
               return_value=_mock_response()):
        result = agent.get_oi_data("SOL/USDT")

    assert result is not None
    assert result["source"] == "coinglass"   # came from a real fetch


# ── CG-07 ─────────────────────────────────────────────────────────────────

def test_cg07_oi_change_computed_from_history():
    agent = _make_fresh_agent()
    now = time.time()

    # Seed history: OI was 1B one hour ago, now 1.1B (+10%)
    with agent._lock:
        hist = agent._history.setdefault("BTC/USDT", deque(maxlen=12))
        hist.append((now - 3600, 1_000_000_000.0))  # 1h ago
        hist.append((now, 1_100_000_000.0))           # now
        agent._cache["BTC/USDT"] = {"oi_usd": 1_100_000_000.0, "ts": now}

    result = agent._build_result("BTC/USDT", agent._cache["BTC/USDT"], source="cached")
    assert result["oi_change_1h_pct"] == pytest.approx(10.0, abs=0.5)


# ── CG-08 ─────────────────────────────────────────────────────────────────

def test_cg08_oi_change_zero_with_single_history_point():
    agent = _make_fresh_agent()
    now = time.time()
    with agent._lock:
        hist = agent._history.setdefault("XRP/USDT", deque(maxlen=12))
        hist.append((now, 500_000_000.0))
        agent._cache["XRP/USDT"] = {"oi_usd": 500_000_000.0, "ts": now}

    result = agent._build_result("XRP/USDT", agent._cache["XRP/USDT"], source="cached")
    assert result["oi_change_1h_pct"] == pytest.approx(0.0, abs=0.01)


# ── CG-09 ─────────────────────────────────────────────────────────────────

def test_cg09_parse_oi_well_formed():
    agent = _make_fresh_agent()
    payload = {
        "code": "0",
        "data": [
            {"exchangeName": "Binance", "openInterestAmount": 800_000_000},
            {"exchangeName": "Bybit",   "openInterestAmount": 200_000_000},
        ]
    }
    result = agent._parse_oi(payload, "BTC")
    assert result == pytest.approx(1_000_000_000.0)


# ── CG-10 ─────────────────────────────────────────────────────────────────

def test_cg10_parse_oi_error_code():
    agent = _make_fresh_agent()
    payload = {"code": "50001", "msg": "Invalid API key", "data": []}
    assert agent._parse_oi(payload, "BTC") is None


# ── CG-11 ─────────────────────────────────────────────────────────────────

def test_cg11_parse_oi_empty_data():
    agent = _make_fresh_agent()
    payload = {"code": "0", "data": []}
    assert agent._parse_oi(payload, "BTC") is None


# ── CG-12 ─────────────────────────────────────────────────────────────────

def test_cg12_timeout_returns_none():
    import requests as _req
    agent = _make_fresh_agent()
    with patch.object(agent, "_load_api_key", return_value="FAKE_KEY"), \
         patch("core.agents.coinglass_agent.requests.get",
               side_effect=_req.exceptions.Timeout):
        result = agent.get_oi_data("BNB/USDT")
    assert result is None


# ── CG-13 ─────────────────────────────────────────────────────────────────

def test_cg13_http_401_returns_none():
    import requests as _req
    agent = _make_fresh_agent()
    err_resp = MagicMock()
    err_resp.status_code = 401
    http_err = _req.exceptions.HTTPError(response=err_resp)

    with patch.object(agent, "_load_api_key", return_value="BAD_KEY"), \
         patch("core.agents.coinglass_agent.requests.get",
               side_effect=http_err):
        result = agent.get_oi_data("BNB/USDT")
    assert result is None


# ── CG-14 ─────────────────────────────────────────────────────────────────

def test_cg14_assess_quality_3_for_fresh_data():
    """assess_oi_data_quality returns quality=3 when fresh data present."""
    from core.signals.oi_signal import assess_oi_data_quality
    from core.agents.coinglass_agent import coinglass_agent

    fresh_data = {
        "oi_change_1h_pct": 1.5,
        "age_seconds": 10.0,
        "raw_oi_usd": 1_000_000_000.0,
        "source": "coinglass",
    }
    with patch.object(coinglass_agent, "get_oi_data", return_value=fresh_data):
        quality, reason = assess_oi_data_quality("BTC/USDT")

    assert quality == 3
    assert reason == "fresh"


# ── CG-15 ─────────────────────────────────────────────────────────────────

def test_cg15_assess_quality_2_for_stale_data():
    """assess_oi_data_quality returns quality=2 when age > OI_STALE_MINUTES."""
    from core.signals.oi_signal import assess_oi_data_quality, OI_STALE_MINUTES
    from core.agents.coinglass_agent import coinglass_agent

    stale_age = (OI_STALE_MINUTES + 1) * 60 + 10  # just over threshold
    stale_data = {
        "oi_change_1h_pct": 0.5,
        "age_seconds": float(stale_age),
        "raw_oi_usd": 900_000_000.0,
        "source": "cached",
    }
    with patch.object(coinglass_agent, "get_oi_data", return_value=stale_data):
        quality, reason = assess_oi_data_quality("ETH/USDT")

    assert quality == 2
    assert "stale" in reason
