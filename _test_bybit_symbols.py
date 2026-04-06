"""
Standalone test: Can Bybit return OHLCV and ticker data for BCH, HBAR, ICP, XLM?
Tests both live and demo (sandbox) modes with multiple symbol formats.
"""
import ccxt
import traceback

SYMBOLS = ["BCH/USDT", "HBAR/USDT", "ICP/USDT", "XLM/USDT"]

# Also test with :USDT suffix (linear perpetual format)
PERP_SYMBOLS = ["BCH/USDT:USDT", "HBAR/USDT:USDT", "ICP/USDT:USDT", "XLM/USDT:USDT"]

def test_mode(label, sandbox, default_type):
    print(f"\n{'='*60}")
    print(f"  {label}  (sandbox={sandbox}, defaultType={default_type})")
    print(f"{'='*60}")

    ex = ccxt.bybit({"options": {"defaultType": default_type}})
    if sandbox:
        ex.set_sandbox_mode(True)

    # Load markets first
    try:
        markets = ex.load_markets()
        print(f"  Loaded {len(markets)} markets")
    except Exception as e:
        print(f"  FAILED to load markets: {e}")
        return

    # Check which symbols exist in markets
    for sym in SYMBOLS + PERP_SYMBOLS:
        exists = sym in markets
        print(f"  Market '{sym}': {'EXISTS' if exists else 'NOT FOUND'}")

    # Try fetching OHLCV for each symbol format
    for sym in SYMBOLS + PERP_SYMBOLS:
        try:
            ohlcv = ex.fetch_ohlcv(sym, "30m", limit=5)
            if ohlcv:
                last = ohlcv[-1]
                print(f"  OHLCV {sym}: OK — {len(ohlcv)} bars, last close={last[4]}")
            else:
                print(f"  OHLCV {sym}: EMPTY response")
        except Exception as e:
            print(f"  OHLCV {sym}: FAILED — {type(e).__name__}: {e}")

    # Try fetching tickers
    print()
    for sym in SYMBOLS + PERP_SYMBOLS:
        try:
            ticker = ex.fetch_ticker(sym)
            print(f"  Ticker {sym}: OK — bid={ticker.get('bid')} ask={ticker.get('ask')} vol={ticker.get('quoteVolume')}")
        except Exception as e:
            print(f"  Ticker {sym}: FAILED — {type(e).__name__}: {e}")

# Test 1: Live spot
test_mode("LIVE SPOT", sandbox=False, default_type="spot")

# Test 2: Live swap (perpetual)
test_mode("LIVE SWAP (perpetual)", sandbox=False, default_type="swap")

# Test 3: Demo swap (what NexusTrader uses)
test_mode("DEMO SWAP (sandbox=True)", sandbox=True, default_type="swap")

# Test 4: Demo spot
test_mode("DEMO SPOT (sandbox=True)", sandbox=True, default_type="spot")
