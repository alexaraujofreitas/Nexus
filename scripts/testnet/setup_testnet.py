#!/usr/bin/env python
"""
NexusTrader — Bybit Testnet Setup Script
=========================================
Configures (or updates) a Bybit testnet exchange row in the database.

Usage:
    python scripts/testnet/setup_testnet.py --api-key YOUR_KEY --api-secret YOUR_SECRET
    python scripts/testnet/setup_testnet.py --from-env   # reads BYBIT_TESTNET_KEY / BYBIT_TESTNET_SECRET

The script:
  1. Deactivates any currently active exchange rows
  2. Creates (or updates) a 'Bybit Testnet' exchange with sandbox_mode=True
  3. Sets it as the active exchange
  4. Validates the connection by calling load_markets()

After running this, start NexusTrader normally. ExchangeManager will load
the testnet credentials and route to api-testnet.bybit.com.
"""

import argparse
import os
import sys

# Add project root to path
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)


def _encrypt(value: str) -> str:
    """Encrypt a string using the project Fernet key, or return plaintext if no key."""
    try:
        from cryptography.fernet import Fernet
        from config.constants import DATA_DIR
        key_file = DATA_DIR / ".nexus_key"
        if not key_file.exists():
            # Generate a new key
            key = Fernet.generate_key()
            key_file.write_bytes(key)
        key = key_file.read_bytes()
        return Fernet(key).encrypt(value.encode()).decode()
    except ImportError:
        return value


def main():
    parser = argparse.ArgumentParser(description="Configure Bybit Testnet for NexusTrader")
    parser.add_argument("--api-key", help="Bybit testnet API key")
    parser.add_argument("--api-secret", help="Bybit testnet API secret")
    parser.add_argument("--from-env", action="store_true",
                        help="Read from BYBIT_TESTNET_KEY / BYBIT_TESTNET_SECRET env vars")
    parser.add_argument("--validate", action="store_true", default=True,
                        help="Validate connection after setup (default: True)")
    parser.add_argument("--no-validate", action="store_true",
                        help="Skip connection validation")
    args = parser.parse_args()

    # Resolve credentials
    api_key = args.api_key
    api_secret = args.api_secret

    if args.from_env:
        api_key = os.environ.get("BYBIT_TESTNET_KEY", "")
        api_secret = os.environ.get("BYBIT_TESTNET_SECRET", "")

    if not api_key or not api_secret:
        print("ERROR: API key and secret are required.")
        print("  Use: --api-key KEY --api-secret SECRET")
        print("  Or:  --from-env  (set BYBIT_TESTNET_KEY and BYBIT_TESTNET_SECRET)")
        sys.exit(1)

    print("=" * 60)
    print("NexusTrader — Bybit Testnet Setup")
    print("=" * 60)

    # ── Step 1: Database setup ────────────────────────────────
    from core.database.engine import get_session
    from core.database.models import Exchange as ExchangeModel

    with get_session() as session:
        # Deactivate all existing exchanges
        active = session.query(ExchangeModel).filter_by(is_active=True).all()
        for ex in active:
            ex.is_active = False
            print(f"  Deactivated: {ex.name} ({ex.mode})")

        # Find or create testnet row
        testnet = (
            session.query(ExchangeModel)
            .filter_by(exchange_id="bybit", sandbox_mode=True)
            .first()
        )
        if testnet:
            print(f"  Updating existing testnet row (id={testnet.id})")
        else:
            testnet = ExchangeModel()
            session.add(testnet)
            print("  Creating new testnet exchange row")

        testnet.name = "Bybit Testnet"
        testnet.exchange_id = "bybit"
        testnet.sandbox_mode = True
        testnet.demo_mode = False
        testnet.is_active = True
        testnet.api_key_encrypted = _encrypt(api_key)
        testnet.api_secret_encrypted = _encrypt(api_secret)
        testnet.api_passphrase_encrypted = ""

        session.flush()
        print(f"  ✓ Bybit Testnet configured (id={testnet.id}, sandbox_mode=True, is_active=True)")

    # ── Step 2: Backup to vault ───────────────────────────────
    try:
        from core.security.key_vault import key_vault
        key_vault.save("exchange.bybit_testnet.api_key", api_key)
        key_vault.save("exchange.bybit_testnet.api_secret", api_secret)
        print("  ✓ Credentials backed up to vault")
    except Exception as e:
        print(f"  ⚠ Vault backup skipped: {e}")

    # ── Step 3: Validate connection ───────────────────────────
    if args.no_validate:
        print("\n  Skipping validation (--no-validate)")
        print("\n✓ Setup complete. Start NexusTrader to connect to Bybit Testnet.")
        return

    print("\n  Validating connection to api-testnet.bybit.com...")
    try:
        import ccxt
        exchange = ccxt.bybit({
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "timeout": 15000,
            "sandbox": True,
        })
        exchange.load_markets()
        n_markets = len(exchange.markets)

        # Test balance fetch
        balance = exchange.fetch_balance()
        usdt_free = balance.get("USDT", {}).get("free", 0)
        usdt_total = balance.get("USDT", {}).get("total", 0)

        # Test ticker fetch
        ticker = exchange.fetch_ticker("BTC/USDT")
        btc_price = ticker.get("last", 0)

        print(f"  ✓ Connected: {n_markets} markets loaded")
        print(f"  ✓ Balance: {usdt_free:.2f} USDT free / {usdt_total:.2f} USDT total")
        print(f"  ✓ BTC/USDT price: ${btc_price:,.2f}")

        # Check watchlist symbols
        watchlist = [
            "BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT", "SOL/USDT",
            "TRX/USDT", "DOGE/USDT", "ADA/USDT", "BCH/USDT", "HYPE/USDT",
            "LINK/USDT", "XLM/USDT", "AVAX/USDT", "HBAR/USDT", "SUI/USDT",
            "NEAR/USDT", "ICP/USDT", "ONDO/USDT", "ALGO/USDT", "RENDER/USDT",
        ]
        available = [s for s in watchlist if s in exchange.markets]
        missing = [s for s in watchlist if s not in exchange.markets]
        print(f"  ✓ Watchlist: {len(available)}/{len(watchlist)} available: {', '.join(available)}")
        if missing:
            print(f"  ⚠ Missing on testnet: {', '.join(missing)}")

    except ccxt.AuthenticationError as e:
        print(f"\n  ✗ Authentication FAILED: {e}")
        print("    Check your API key and secret. Bybit testnet keys are generated at:")
        print("    https://testnet.bybit.com/app/user/api-management")
        sys.exit(1)
    except Exception as e:
        print(f"\n  ✗ Connection failed: {e}")
        sys.exit(1)

    print("\n" + "=" * 60)
    print("✓ Bybit Testnet setup complete!")
    print("  Start NexusTrader, then switch to Live mode in Risk Management page.")
    print("  The system will connect to api-testnet.bybit.com automatically.")
    print("=" * 60)


if __name__ == "__main__":
    main()
