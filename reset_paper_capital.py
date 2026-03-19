#!/usr/bin/env python3
"""
reset_paper_capital.py
======================
Clears all paper-trading history and resets capital to $100,000.

Run ONLY while NexusTrader is closed.
"""

import json
import sqlite3
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent
DB_PATH    = BASE_DIR / "data" / "nexus_trader.db"
JSON_PATH  = BASE_DIR / "data" / "open_positions.json"
OUTCOMES_PATH = BASE_DIR / "data" / "trade_outcomes.jsonl"
OUTCOME_TRACKER_PATH = BASE_DIR / "data" / "outcome_tracker.json"
LEVEL2_PATH = BASE_DIR / "data" / "level2_tracker.json"
WAL_PATH   = Path(str(DB_PATH) + "-wal")

RESET_CAPITAL = 100_000.0

# ── Safety check ─────────────────────────────────────────────────────────────
if WAL_PATH.exists() and WAL_PATH.stat().st_size > 0:
    print("WARNING: SQLite WAL file is non-empty.")
    print("   This may mean NexusTrader is still running.")
    print("   Close the application completely before running this script.")
    print("   If you are sure it is closed, the WAL will be applied automatically.")
    print()

# ── Step 1: Clear SQLite paper_trades ────────────────────────────────────────
print("Step 1: Clearing paper_trades table...")
try:
    conn = sqlite3.connect(str(DB_PATH))
    cur  = conn.cursor()

    # Count rows before deletion
    cur.execute("SELECT COUNT(*) FROM paper_trades")
    count = cur.fetchone()[0]
    print(f"  Found {count} rows in paper_trades.")

    cur.execute("DELETE FROM paper_trades")
    conn.commit()

    # Force WAL checkpoint so the DB file itself is updated
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    print(f"  Deleted {count} rows. WAL checkpointed.")
except sqlite3.OperationalError as e:
    print(f"  SQLite error: {e}")
    raise

# ── Step 2: Reset open_positions.json ────────────────────────────────────────
print("\nStep 2: Resetting open_positions.json to $100,000...")
clean_state = {
    "capital":      RESET_CAPITAL,
    "peak_capital": RESET_CAPITAL,
    "positions":    []
}
with open(JSON_PATH, "w") as f:
    json.dump(clean_state, f, indent=2)
print(f"  Capital reset to ${RESET_CAPITAL:,.2f}. 0 open positions.")

# ── Step 3: Clear trade_outcomes.jsonl ───────────────────────────────────────
print("\nStep 3: Clearing trade_outcomes.jsonl (Level-2 learning history)...")
if OUTCOMES_PATH.exists():
    OUTCOMES_PATH.write_text("")
    print("  trade_outcomes.jsonl cleared.")
else:
    print("  (file not found - skipping)")

# ── Step 4: Reset outcome_tracker.json ───────────────────────────────────────
print("\nStep 4: Resetting outcome_tracker.json (adaptive weights)...")
if OUTCOME_TRACKER_PATH.exists():
    OUTCOME_TRACKER_PATH.write_text("{}")
    print("  outcome_tracker.json reset.")
else:
    print("  (file not found - skipping)")

# ── Step 5: Reset level2_tracker.json ────────────────────────────────────────
print("\nStep 5: Resetting level2_tracker.json (Level-2 contextual learning)...")
if LEVEL2_PATH.exists():
    LEVEL2_PATH.write_text("{}")
    print("  level2_tracker.json reset.")
else:
    print("  (file not found - skipping)")

# ── Done ─────────────────────────────────────────────────────────────────────
print()
print("=" * 60)
print("Reset complete. NexusTrader is ready for a fresh start.")
print(f"   Starting capital: ${RESET_CAPITAL:,.2f}")
print("   Open positions:    0")
print("   Trade history:     cleared")
print("   Adaptive weights:  reset")
print("=" * 60)
