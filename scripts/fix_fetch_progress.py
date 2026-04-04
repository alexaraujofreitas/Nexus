"""
scripts/fix_fetch_progress.py
──────────────────────────────
Run this ONCE after the first fetch_historical_data.py run to:
  1. Clear funding_done flags for symbols where 0 records were fetched
     (caused by the old spot-symbol bug — now fixed)
  2. Clear oi_done flags for symbols where < 50,000 OI records were fetched
     (caused by the old timeout-break bug — now fixed)

This allows the next --resume run to re-fetch only the failed/incomplete data.

Usage:
    python scripts/fix_fetch_progress.py
"""
import json
import sys
from pathlib import Path

import pandas as pd

ROOT_DIR    = Path(__file__).parent.parent
OUT_DIR     = ROOT_DIR / "data" / "validation"
PROGRESS_F  = OUT_DIR / "fetch_progress.json"

SYMBOLS = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
}

MIN_OI_RECORDS      = 10_000   # below this → consider incomplete
                               # (1h resolution: 730 days × 24 = 17,520 expected)
MIN_FUNDING_RECORDS = 100      # below this → consider incomplete


def main() -> None:
    if not PROGRESS_F.exists():
        print("No progress file found — nothing to fix.")
        return

    progress = json.loads(PROGRESS_F.read_text())
    changes  = []

    for short_sym, slug in SYMBOLS.items():
        # ── Funding check ───────────────────────────────────────────────────
        funding_key  = f"{slug}_funding_done"
        funding_file = OUT_DIR / f"{slug}_funding.parquet"
        if progress.get(funding_key):
            count = 0
            if funding_file.exists():
                try:
                    count = len(pd.read_parquet(funding_file))
                except Exception:
                    count = 0
            if count < MIN_FUNDING_RECORDS:
                del progress[funding_key]
                changes.append(
                    f"  [{short_sym}] Cleared funding_done ({count} records < {MIN_FUNDING_RECORDS})"
                )

        # ── OI check ────────────────────────────────────────────────────────
        oi_key  = f"{slug}_oi_done"
        oi_file = OUT_DIR / f"{slug}_oi.parquet"
        if progress.get(oi_key):
            count = 0
            if oi_file.exists():
                try:
                    count = len(pd.read_parquet(oi_file))
                except Exception:
                    count = 0
            if count < MIN_OI_RECORDS:
                del progress[oi_key]
                changes.append(
                    f"  [{short_sym}] Cleared oi_done ({count} records < {MIN_OI_RECORDS})"
                )

    if not changes:
        print("All funding and OI records look complete — no changes needed.")
        return

    PROGRESS_F.write_text(json.dumps(progress, indent=2))
    print("Progress file updated:")
    for c in changes:
        print(c)
    print()
    print("Now re-run the fetcher with --resume to collect the missing data:")
    print("  python scripts/fetch_historical_data.py --symbols BTC ETH SOL --years 4 --exchange bybit --resume")


if __name__ == "__main__":
    main()
