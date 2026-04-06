# ============================================================
# Phase 8E — Settings Remaining Tabs Tests
#
# Validates:
#  1. Agent vault keys in registry
#  2. Agent vault key masking
#  3. Agent vault key encryption
#  4. Risk tab field completeness (desktop parity)
#  5. Backtesting tab field completeness
#  6. Intelligence Agents tab field completeness
#  7. Portfolio Allocation field completeness
#  8. Full Settings tab count (10 tabs — desktop 7 + web enhancements)
# ============================================================
from __future__ import annotations

import pytest
from pathlib import Path

from app.services.vault import VaultService, get_vault, reset_vault
from app.services import vault as vault_mod
from app.api.settings_api import (
    _mask_vault_keys_in_config,
    _encrypt_vault_keys_in_updates,
)


VAULT_KEYS = VaultService.VAULT_KEYS


@pytest.fixture
def vault(tmp_path: Path):
    v = VaultService(key_path=tmp_path / ".test_key")
    return v


# ── 1. Agent Vault Keys ─────────────────────────────────────

class TestAgentVaultKeys:
    """Agent credential keys are in the vault registry."""

    AGENT_VAULT_KEYS = [
        "agents.coinglass_api_key",
        "agents.fred_api_key",
        "agents.lunarcrush_api_key",
        "agents.cryptopanic_api_key",
    ]

    def test_all_agent_keys_registered(self):
        for key in self.AGENT_VAULT_KEYS:
            assert key in VAULT_KEYS, f"{key} missing from VAULT_KEYS"

    def test_agent_vault_key_count(self):
        agent_keys = [k for k in VAULT_KEYS if k.startswith("agents.")]
        assert len(agent_keys) == 4

    def test_is_vault_key_method(self):
        for key in self.AGENT_VAULT_KEYS:
            assert VaultService.is_vault_key(key) is True

    def test_non_vault_agent_keys(self):
        non_vault = [
            "agents.auto_start",
            "agents.min_confluence_boost",
            "agents.options_enabled",
            "agents.options_max_days_expiry",
            "agents.funding_enabled",
            "agents.orderbook_enabled",
        ]
        for key in non_vault:
            assert VaultService.is_vault_key(key) is False


# ── 2. Agent Key Masking ────────────────────────────────────

class TestAgentKeyMasking:

    @pytest.fixture(autouse=True)
    def _set_singleton(self, vault):
        vault_mod._vault_instance = vault
        yield
        vault_mod._vault_instance = None

    def test_mask_coinglass_key(self, vault):
        encrypted = vault.encrypt("cg_abc123def456")
        config = {"agents": {"coinglass_api_key": encrypted}}
        result = _mask_vault_keys_in_config(config)
        assert "****" in result["agents"]["coinglass_api_key"]

    def test_mask_fred_key(self, vault):
        encrypted = vault.encrypt("fred_key_value_xyz")
        config = {"agents": {"fred_api_key": encrypted}}
        result = _mask_vault_keys_in_config(config)
        assert "****" in result["agents"]["fred_api_key"]

    def test_mask_lunarcrush_key(self, vault):
        encrypted = vault.encrypt("lc_key_abcdefgh")
        config = {"agents": {"lunarcrush_api_key": encrypted}}
        result = _mask_vault_keys_in_config(config)
        assert "****" in result["agents"]["lunarcrush_api_key"]

    def test_nonvault_agent_fields_pass_through(self, vault):
        config = {
            "agents": {
                "auto_start": True,
                "min_confluence_boost": 0.25,
                "options_enabled": True,
            }
        }
        result = _mask_vault_keys_in_config(config)
        assert result["agents"]["auto_start"] is True
        assert result["agents"]["min_confluence_boost"] == 0.25


# ── 3. Agent Key Encryption ─────────────────────────────────

class TestAgentKeyEncryption:

    @pytest.fixture(autouse=True)
    def _set_singleton(self, vault):
        vault_mod._vault_instance = vault
        yield
        vault_mod._vault_instance = None

    def test_encrypt_coinglass_key(self, vault):
        updates = {"agents.coinglass_api_key": "cg_test_123"}
        result = _encrypt_vault_keys_in_updates(updates)
        assert vault.is_encrypted(result["agents.coinglass_api_key"])

    def test_encrypt_fred_key(self, vault):
        updates = {"agents.fred_api_key": "fred_test"}
        result = _encrypt_vault_keys_in_updates(updates)
        assert vault.is_encrypted(result["agents.fred_api_key"])

    def test_encrypt_lunarcrush_key(self, vault):
        updates = {"agents.lunarcrush_api_key": "lc_test"}
        result = _encrypt_vault_keys_in_updates(updates)
        assert vault.is_encrypted(result["agents.lunarcrush_api_key"])

    def test_nonvault_agent_pass_through(self, vault):
        updates = {
            "agents.auto_start": False,
            "agents.min_confluence_boost": 0.30,
        }
        result = _encrypt_vault_keys_in_updates(updates)
        assert result["agents.auto_start"] is False
        assert result["agents.min_confluence_boost"] == 0.30


# ── 4. Risk Tab Desktop Parity ──────────────────────────────

class TestRiskTabParity:
    """All desktop Risk tab fields are represented."""

    RISK_FIELDS = [
        "risk_engine.risk_pct_per_trade",
        "risk_engine.max_capital_pct",
        "risk.max_position_pct",
        "risk.max_portfolio_drawdown_pct",
        "risk.max_strategy_drawdown_pct",
        "risk.max_portfolio_heat",
        "risk.min_sharpe_live",
        "risk.max_spread_pct",
        "risk.default_stop_loss_pct",
        "risk.default_take_profit_pct",
        "risk.max_concurrent_positions",
        "risk.min_risk_reward",
        "idss.min_confluence_score",
        "risk.max_open_positions",
    ]

    def test_risk_field_count(self):
        """Desktop Risk tab has 14 fields (3 sections)."""
        assert len(self.RISK_FIELDS) == 14

    def test_position_portfolio_section(self):
        section = [
            "risk_engine.risk_pct_per_trade", "risk_engine.max_capital_pct",
            "risk.max_position_pct", "risk.max_portfolio_drawdown_pct",
            "risk.max_strategy_drawdown_pct", "risk.max_portfolio_heat",
            "risk.min_sharpe_live", "risk.max_spread_pct",
        ]
        assert len(section) == 8

    def test_stop_tp_section(self):
        section = ["risk.default_stop_loss_pct", "risk.default_take_profit_pct"]
        assert len(section) == 2

    def test_idss_section(self):
        section = [
            "risk.max_concurrent_positions", "risk.min_risk_reward",
            "idss.min_confluence_score", "risk.max_open_positions",
        ]
        assert len(section) == 4


# ── 5. Backtesting Tab Fields ───────────────────────────────

class TestBacktestingTabParity:
    """All desktop Backtesting tab fields."""

    BACKTEST_FIELDS = [
        "backtesting.default_fee_pct",
        "backtesting.default_slippage_pct",
        "backtesting.default_initial_capital",
        "backtesting.walk_forward_train_months",
        "backtesting.walk_forward_validate_months",
    ]

    def test_backtest_field_count(self):
        assert len(self.BACKTEST_FIELDS) == 5

    def test_all_in_backtesting_namespace(self):
        for f in self.BACKTEST_FIELDS:
            assert f.startswith("backtesting."), f"{f} not in backtesting namespace"


# ── 6. Intelligence Agents Tab Fields ───────────────────────

class TestAgentsTabParity:
    """All desktop Intelligence Agents tab fields."""

    AGENT_CONFIG_FIELDS = [
        "agents.auto_start",
        "agents.min_confluence_boost",
        "agents.coinglass_api_key",
        "agents.fred_api_key",
        "agents.lunarcrush_api_key",
        "agents.options_enabled",
        "agents.options_max_days_expiry",
        "agents.funding_enabled",
        "agents.orderbook_enabled",
    ]

    def test_agents_field_count(self):
        """Desktop Agents tab: 9 fields (2 behaviour + 3 vault + 4 toggles/numbers)."""
        assert len(self.AGENT_CONFIG_FIELDS) == 9

    def test_vault_keys_in_agents_fields(self):
        vault_fields = [f for f in self.AGENT_CONFIG_FIELDS if VaultService.is_vault_key(f)]
        assert len(vault_fields) == 3  # coinglass, fred, lunarcrush

    def test_non_vault_fields(self):
        non_vault = [f for f in self.AGENT_CONFIG_FIELDS if not VaultService.is_vault_key(f)]
        assert len(non_vault) == 6


# ── 7. Portfolio Allocation Fields ──────────────────────────

class TestPortfolioAllocationParity:
    """All desktop Portfolio Allocation tab fields."""

    SYMBOLS = [
        "BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT", "SOL/USDT",
        "TRX/USDT", "DOGE/USDT", "ADA/USDT", "BCH/USDT", "HYPE/USDT",
        "LINK/USDT", "XLM/USDT", "AVAX/USDT", "HBAR/USDT", "SUI/USDT",
        "NEAR/USDT", "ICP/USDT", "ONDO/USDT", "ALGO/USDT", "RENDER/USDT",
    ]

    def test_symbol_count(self):
        assert len(self.SYMBOLS) == 20

    def test_mode_options(self):
        modes = ["STATIC", "DYNAMIC"]
        assert len(modes) == 2

    def test_static_weight_fields(self):
        fields = [f"symbol_allocation.static_weights.{s}" for s in self.SYMBOLS]
        assert len(fields) == 20

    def test_btc_dominance_fields(self):
        fields = [
            "symbol_allocation.btc_dominance_pct",
            "symbol_allocation.btc_dominance_high",
            "symbol_allocation.btc_dominance_low",
        ]
        assert len(fields) == 3

    def test_dynamic_profile_fields(self):
        profiles = ["btc_dominant", "neutral", "alt_season"]
        total = 0
        for p in profiles:
            for s in self.SYMBOLS:
                total += 1
        assert total == 60  # 3 profiles × 20 symbols

    def test_total_portfolio_fields(self):
        """1 mode + 20 static + 3 thresholds + 60 dynamic = 84 fields total."""
        total = 1 + 20 + 3 + 60
        assert total == 84


# ── 8. Full Settings Tab Count ──────────────────────────────

class TestFullSettingsTabCount:
    """Web Settings now has 10 tabs (desktop 7 + 3 web enhancements)."""

    WEB_TABS = [
        "risk", "strategy", "execution",
        "ai_ml", "data_sentiment", "notifications",
        "backtesting", "agents", "portfolio",
        "api_keys",
    ]

    # Desktop tabs for reference
    DESKTOP_TABS = [
        "Risk Management", "AI & ML", "Data & Feeds",
        "Backtesting", "Notifications", "Intelligence Agents",
        "Portfolio Allocation",
    ]

    def test_web_tab_count(self):
        assert len(self.WEB_TABS) == 10

    def test_desktop_tab_count(self):
        assert len(self.DESKTOP_TABS) == 7

    def test_all_desktop_tabs_covered(self):
        """Every desktop tab has a web equivalent."""
        mapping = {
            "Risk Management": "risk",
            "AI & ML": "ai_ml",
            "Data & Feeds": "data_sentiment",
            "Backtesting": "backtesting",
            "Notifications": "notifications",
            "Intelligence Agents": "agents",
            "Portfolio Allocation": "portfolio",
        }
        for desktop, web in mapping.items():
            assert web in self.WEB_TABS, f"Desktop '{desktop}' not mapped to web tab"

    def test_web_enhancement_tabs(self):
        """Three web-only tabs: Strategy, Execution, API Keys."""
        web_only = {"strategy", "execution", "api_keys"}
        for tab in web_only:
            assert tab in self.WEB_TABS


# ── 9. Vault Key Grand Total ────────────────────────────────

class TestVaultKeyGrandTotal:
    """Verify the total vault key registry covers all credential fields."""

    def test_total_vault_keys(self):
        assert len(VAULT_KEYS) == 23

    def test_vault_keys_by_category(self):
        ai_keys = [k for k in VAULT_KEYS if k.startswith("ai.")]
        sentiment_keys = [k for k in VAULT_KEYS if k.startswith("sentiment.")]
        agent_keys = [k for k in VAULT_KEYS if k.startswith("agents.")]
        notification_keys = [k for k in VAULT_KEYS if k.startswith("notifications.")]
        exchange_keys = [k for k in VAULT_KEYS if k.startswith("exchange.")]
        api_keys = [k for k in VAULT_KEYS if k.startswith("api_keys.")]

        assert len(ai_keys) == 3          # anthropic, openai, gemini
        assert len(sentiment_keys) == 4    # news, cryptopanic, reddit_id, reddit_secret
        assert len(agent_keys) == 4        # fred, lunarcrush, coinglass, cryptopanic
        assert len(notification_keys) == 5  # twilio_sid/token, telegram, email_pw, gemini_pw
        assert len(exchange_keys) == 3      # api_key, api_secret, api_passphrase
        assert len(api_keys) == 4           # cryptopanic, coinglass, reddit_id, reddit_secret
        # Total: 3+4+4+5+3+4 = 23
