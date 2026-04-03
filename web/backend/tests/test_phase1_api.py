# ============================================================
# Phase 1 Tests — FastAPI Application
#
# Tests the API service: health endpoints, auth flow, CORS,
# WebSocket, and engine command routing.
# ============================================================
import sys
import os
import pytest

# Path setup
_BACKEND = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, _BACKEND)

from app.config import Settings, get_settings


class TestConfig:
    """Verify Settings model loads defaults correctly."""

    def test_default_service_name(self):
        s = Settings()
        assert s.service_name == os.getenv("NEXUS_SERVICE_NAME", "api")

    def test_default_database_url(self):
        s = Settings()
        assert "postgresql" in s.database_url or "sqlite" in s.database_url

    def test_default_redis_url(self):
        s = Settings()
        assert s.redis_url.startswith("redis://")

    def test_jwt_defaults(self):
        s = Settings()
        assert s.jwt_algorithm == "HS256"
        assert s.access_token_expire_minutes == 15
        assert s.refresh_token_expire_days == 7

    def test_cors_origins_parsed(self):
        s = Settings()
        assert isinstance(s.cors_origins, list)
        assert len(s.cors_origins) >= 1

    def test_rate_limits(self):
        s = Settings()
        assert s.rate_limit_global == 100
        assert s.rate_limit_auth == 5
        assert s.rate_limit_commands == 10

    def test_get_settings_cached(self):
        """get_settings() should return the same object (lru_cache)."""
        a = get_settings()
        b = get_settings()
        assert a is b


class TestJWT:
    """Verify JWT creation and validation."""

    def test_create_and_decode_access_token(self):
        from app.auth.jwt import create_access_token, decode_access_token

        token = create_access_token({"sub": "42", "email": "test@example.com"})
        payload = decode_access_token(token)
        assert payload is not None
        assert payload["sub"] == "42"
        assert payload["email"] == "test@example.com"
        assert payload["type"] == "access"

    def test_invalid_token_returns_none(self):
        from app.auth.jwt import decode_access_token

        assert decode_access_token("garbage.token.here") is None

    def test_refresh_token_pair(self):
        from app.auth.jwt import create_refresh_token, hash_refresh_token

        raw, token_hash = create_refresh_token()
        assert len(raw) > 20
        assert len(token_hash) == 64  # SHA-256 hex
        assert hash_refresh_token(raw) == token_hash

    def test_password_hashing(self):
        from app.auth.jwt import hash_password, verify_password

        hashed = hash_password("mysecret")
        assert verify_password("mysecret", hashed)
        assert not verify_password("wrongpassword", hashed)


class TestDatabaseModule:
    """Verify database.py factory functions don't crash on import."""

    def test_base_exists(self):
        from app.database import Base
        assert Base is not None
        assert hasattr(Base, "metadata")

    def test_sync_url_builder(self):
        from app.database import _build_sync_url
        assert _build_sync_url("postgresql+asyncpg://host/db") == "postgresql://host/db"
        assert _build_sync_url("postgresql://host/db") == "postgresql://host/db"

    def test_async_url_builder(self):
        from app.database import _build_async_url
        assert _build_async_url("postgresql://host/db") == "postgresql+asyncpg://host/db"
        assert _build_async_url("postgresql+asyncpg://host/db") == "postgresql+asyncpg://host/db"


class TestModelsSchema:
    """Verify all ORM models register with Base.metadata."""

    def test_all_tables_registered(self):
        from app.database import Base
        import app.models  # noqa: F401

        table_names = set(Base.metadata.tables.keys())

        expected_tables = {
            "exchanges", "assets", "ohlcv", "features",
            "strategies", "strategy_metrics", "signals",
            "trades", "orders", "positions",
            "sentiment_data", "ml_models", "model_predictions",
            "market_regimes", "portfolio_snapshots",
            "backtest_results", "paper_trades", "live_trades",
            "settings", "system_logs",
            "agent_signals", "signal_log",
            "trade_feedback", "strategy_tuning_proposals",
            "applied_strategy_changes", "tuning_proposal_outcomes",
            # Web-only tables
            "web_users", "web_refresh_tokens",
        }

        for table in expected_tables:
            assert table in table_names, f"Missing table: {table}"

    def test_table_count(self):
        """Verify we have at least 28 tables (26 trading + 2 auth)."""
        from app.database import Base
        import app.models  # noqa: F401

        assert len(Base.metadata.tables) >= 28

    def test_paper_trade_to_dict(self):
        from app.models.trading import PaperTrade
        pt = PaperTrade(
            symbol="BTC/USDT", side="buy", entry_price=50000.0,
            exit_price=51000.0, size_usdt=100.0, pnl_usdt=2.0,
            pnl_pct=2.0, opened_at="2026-01-01T00:00:00",
            closed_at="2026-01-01T01:00:00",
        )
        d = pt.to_dict()
        assert d["symbol"] == "BTC/USDT"
        assert d["entry_size_usdt"] == 100.0  # Falls back to size_usdt

    def test_paper_trade_to_dict_with_entry_exit_sizes(self):
        from app.models.trading import PaperTrade
        pt = PaperTrade(
            symbol="ETH/USDT", side="buy", entry_price=3000.0,
            exit_price=3100.0, size_usdt=200.0,
            entry_size_usdt=200.0, exit_size_usdt=66.0,
            pnl_usdt=2.2, pnl_pct=3.3,
            opened_at="2026-01-01T00:00:00",
            closed_at="2026-01-01T02:00:00",
        )
        d = pt.to_dict()
        assert d["entry_size_usdt"] == 200.0
        assert d["exit_size_usdt"] == 66.0


class TestWebSocketManager:
    """Verify WebSocket manager subscription logic."""

    def test_channel_validation(self):
        from app.ws.manager import CHANNELS
        assert "ticker" in CHANNELS
        assert "positions" in CHANNELS
        assert "trades" in CHANNELS
        assert "scanner" in CHANNELS
        assert "engine" in CHANNELS
        assert "alerts" in CHANNELS

    def test_connection_manager_init(self):
        from app.ws.manager import ConnectionManager
        mgr = ConnectionManager()
        assert mgr.connection_count == 0

    def test_subscribe_unsubscribe(self):
        from app.ws.manager import ConnectionManager
        mgr = ConnectionManager()
        # Fake a connection
        mgr._connections["test1"] = "fake_ws"
        mgr.subscribe("test1", "ticker")
        assert "test1" in mgr._subscriptions.get("ticker", set())
        mgr.unsubscribe("test1", "ticker")
        assert "test1" not in mgr._subscriptions.get("ticker", set())

    def test_subscribe_invalid_channel_ignored(self):
        from app.ws.manager import ConnectionManager
        mgr = ConnectionManager()
        mgr._connections["test1"] = "fake_ws"
        mgr.subscribe("test1", "nonexistent_channel")
        assert "nonexistent_channel" not in mgr._subscriptions

    def test_disconnect_cleans_subscriptions(self):
        from unittest.mock import MagicMock
        from app.ws.manager import ConnectionManager, _ConnState
        mgr = ConnectionManager()
        state = _ConnState(ws=MagicMock(), user_sub="u1", email="t@t.com", token="tok")
        mgr._connections["test1"] = state
        mgr._user_counts["u1"] = 1
        mgr.subscribe("test1", "ticker")
        mgr.subscribe("test1", "trades")
        mgr.disconnect("test1")
        assert mgr.connection_count == 0
        assert "test1" not in mgr._subscriptions.get("ticker", set())
        assert "test1" not in mgr._subscriptions.get("trades", set())


class TestFastAPIApp:
    """Verify FastAPI app object is created correctly."""

    def test_app_import(self):
        from main import app
        assert app is not None
        assert app.title == "NexusTrader API"

    def test_routes_registered(self):
        from main import app
        route_paths = [r.path for r in app.routes]
        assert "/" in route_paths
        assert "/health" in route_paths
        assert "/health/ready" in route_paths
        assert "/api/v1/auth/login" in route_paths
        assert "/api/v1/auth/setup" in route_paths
        assert "/api/v1/auth/refresh" in route_paths
        assert "/api/v1/auth/me" in route_paths
        assert "/api/v1/engine/status" in route_paths
        assert "/api/v1/engine/command" in route_paths
        assert "/ws" in route_paths


class TestAlembicEnvImport:
    """Verify Alembic env.py can load target_metadata."""

    def test_metadata_has_tables(self):
        from app.database import Base
        import app.models  # noqa: F401
        # This is what alembic/env.py does
        assert len(Base.metadata.tables) >= 28


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
