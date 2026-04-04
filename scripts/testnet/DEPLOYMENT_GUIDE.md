# NexusTrader — Bybit Testnet Deployment Guide

## Prerequisites

- NexusTrader installed and running (paper trading confirmed working)
- Bybit testnet account at https://testnet.bybit.com
- Testnet API key + secret from https://testnet.bybit.com/app/user/api-management
- Python environment with all NexusTrader dependencies installed

## Step-by-Step Deployment

### Step 1: Generate Testnet API Credentials

1. Go to https://testnet.bybit.com and create/log in to your account
2. Navigate to API Management: https://testnet.bybit.com/app/user/api-management
3. Create a new API key with these permissions:
   - **Read-Write** access
   - **Unified Trading** enabled
   - IP restriction: leave unrestricted for testnet
4. Copy the API Key and Secret — you'll need them in Step 2

### Step 2: Configure Testnet in NexusTrader

Run the setup script from the project root:

```bash
python scripts/testnet/setup_testnet.py --api-key YOUR_KEY --api-secret YOUR_SECRET
```

Or use environment variables:

```bash
set BYBIT_TESTNET_KEY=YOUR_KEY
set BYBIT_TESTNET_SECRET=YOUR_SECRET
python scripts/testnet/setup_testnet.py --from-env
```

Expected output:
```
NexusTrader — Bybit Testnet Setup
  Creating new testnet exchange row
  Bybit Testnet configured (id=X, sandbox_mode=True, is_active=True)
  Credentials backed up to vault
  Validating connection to api-testnet.bybit.com...
  Connected: XXXX markets loaded
  Balance: XXXX.XX USDT free / XXXX.XX USDT total
  BTC/USDT price: $XX,XXX.XX
  Watchlist: 5/5 available
  Bybit Testnet setup complete!
```

### Step 3: Validate Connection

Run the pre-flight validator:

```bash
python scripts/testnet/validate_connection.py
```

All 7 checks must pass before proceeding:
- Database row exists
- CCXT connects to testnet
- Balance >= $50 USDT
- Watchlist symbols available
- Order permissions work
- Lifecycle logger writable
- LiveExecutor constructs

### Step 4: Start NexusTrader

1. Launch NexusTrader normally
2. Verify the sidebar shows **"SANDBOX / TESTNET"** in gold
3. Navigate to Risk Management page
4. Switch to **Live** mode (the confirmation dialog will appear)
5. Confirm — the system will route to api-testnet.bybit.com (not real money)

### Step 5: Run Edge-Case Test Harness

With NexusTrader running in Live+Testnet mode:

```bash
# Run all 8 scenarios
python scripts/testnet/test_harness.py --scenario all

# Or run specific scenarios
python scripts/testnet/test_harness.py --scenario 1,3,5,7
```

Scenarios tested:
1. Normal buy + manual close (full lifecycle)
2. Normal sell + manual close
3. Partial close (50%) + dust threshold (99.5%)
4. Rapid open/close (latency stress)
5. Multi-symbol simultaneous opens (3 positions)
6. SL/TP geometry rejection (invalid candidate)
7. Double-close rejection (state machine)
8. Pre-trade gate: exchange disconnected simulation

Results saved to `data/test_harness_results.json`.

### Step 6: Accumulate 50+ Scanner Trades

Leave NexusTrader running with `scanner.auto_execute: true` (default).
The IDSS scanner will generate signals and execute trades autonomously.

Monitor progress:
- Dashboard shows real-time position count and P&L
- `data/trade_lifecycle.jsonl` accumulates events
- Check trade count: `wc -l data/trade_lifecycle.jsonl`

Estimated time: 6-24 hours depending on market conditions and signal frequency.

### Step 7: Generate Validation Report

After 50+ trades:

```bash
python scripts/testnet/generate_report.py
```

This produces:
- Console summary with P&L, latency, slippage, and per-symbol breakdown
- `data/testnet_validation_report.json` with full data

### Step 8: Review and Approve

Review the report for:
- **Success rate**: All test harness scenarios passed
- **Execution accuracy**: Latency < 5s, slippage < 10 bps average
- **No anomalies**: Error rate < 5%, no orphaned positions
- **State machine integrity**: No double-closes, proper close lifecycle
- **Reconnection handling**: If any disconnects occurred, reconciliation worked

## Troubleshooting

### "Authentication FAILED"
- Verify testnet keys (not mainnet keys)
- Regenerate at https://testnet.bybit.com/app/user/api-management

### "No markets loaded"
- Bybit testnet may be undergoing maintenance
- Try again in 30 minutes

### Low balance
- Bybit testnet provides free test USDT
- Check your testnet wallet at https://testnet.bybit.com

### VPN issues
- Japan VPN causes 403 errors on Bybit
- Switch to a US/EU VPN endpoint

### Scanner not generating trades
- Verify `scanner.auto_execute: true` in config.yaml
- Check that Live mode is active (not Paper mode)
- Monitor IDSS Scanner page for signal activity

## File Reference

| File | Purpose |
|------|---------|
| `scripts/testnet/setup_testnet.py` | Configure testnet credentials |
| `scripts/testnet/validate_connection.py` | Pre-flight connection check |
| `scripts/testnet/test_harness.py` | Edge-case scenario testing |
| `scripts/testnet/generate_report.py` | Post-run validation report |
| `core/execution/trade_lifecycle_logger.py` | JSONL event logger |
| `core/execution/live_executor.py` | Live order executor (instrumented) |
| `data/trade_lifecycle.jsonl` | Raw lifecycle events |
| `data/test_harness_results.json` | Harness scenario results |
| `data/testnet_validation_report.json` | Final validation report |
