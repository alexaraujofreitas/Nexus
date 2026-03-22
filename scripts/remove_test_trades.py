"""
remove_test_trades.py
---------------------
Removes the two BTC/USDT test trades from the paper_trades database.
These were created by the "Test Position" button during AT-12/AT-13
acceptance testing (rationale="Test signal", duration_s=0).

Run this ONLY when NexusTrader is closed, then restart the app.
Capital will correctly show $100,000.00 after the cleanup.

Usage:
    python scripts/remove_test_trades.py
"""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "nexus_trader.db")


def main():
    print(f"Database: {DB_PATH}")
    if not os.path.exists(DB_PATH):
        print("ERROR: DB not found.")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, symbol, side, pnl_usdt, opened_at, rationale, duration_s "
            "FROM paper_trades WHERE rationale = 'Test signal' AND duration_s = 0"
        ).fetchall()

        if not rows:
            print("No test trades found. Nothing to remove.")
            return

        print(f"\nFound {len(rows)} test trade(s) to remove:")
        for r in rows:
            print(
                f"  id={r['id']} | {r['symbol']} {r['side']} | "
                f"pnl={r['pnl_usdt']:.2f} | opened={r['opened_at']} | "
                f"rationale='{r['rationale']}'"
            )

        confirm = input("\nRemove these trades? (yes/no): ").strip().lower()
        if confirm != "yes":
            print("Aborted.")
            return

        conn.execute(
            "DELETE FROM paper_trades WHERE rationale = 'Test signal' AND duration_s = 0"
        )
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
        print(f"\nRemoved {len(rows)} test trade(s). Capital will show $100,000.00 on next startup.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
