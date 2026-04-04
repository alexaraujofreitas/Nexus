"""
scripts/api_server.py — NexusTrader Orders & Positions Data Bridge
===================================================================
Minimal local HTTP API that exposes live NexusTrader state to the
orders_positions.html dashboard. Zero external dependencies (stdlib only).

DATA SOURCES (read-only, non-invasive):
  Open positions  → data/open_positions.json   (written by PaperExecutor)
  Closed trades   → data/nexus_trader.db        paper_trades table (authoritative)
  Active orders   → data/nexus_trader.db        orders table
  Account summary → computed from above sources

ENDPOINTS:
  GET /api/health     → {"status":"ok", "db":true/false, "timestamp":"..."}
  GET /api/summary    → account metrics (capital, P&L, win rate, drawdown …)
  GET /api/positions  → open positions from open_positions.json
  GET /api/trades     → closed trades from paper_trades (query: limit, offset, symbol, side)
  GET /api/orders     → all orders from orders table

USAGE:
  python scripts/api_server.py
  python scripts/api_server.py --port 7432 --host 127.0.0.1

Then open orders_positions.html in your browser.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("api_server")

ROOT_DIR   = Path(__file__).resolve().parent.parent
DATA_DIR   = ROOT_DIR / "data"
DB_PATH    = DATA_DIR / "nexus_trader.db"
POS_FILE   = DATA_DIR / "open_positions.json"
HTML_FILE  = ROOT_DIR / "orders_positions.html"

DEFAULT_PORT = 7432
DEFAULT_HOST = "0.0.0.0"   # listen on all interfaces → accessible from phone on same WiFi

_INITIAL_CAPITAL = 100_000.0  # Phase 1 demo start capital (CLAUDE.md)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _cors() -> dict[str, str]:
    return {
        "Access-Control-Allow-Origin":  "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Content-Type":                 "application/json; charset=utf-8",
        "Cache-Control":                "no-cache, no-store, must-revalidate",
    }


def _db_open() -> sqlite3.Connection | None:
    """Open a read-only SQLite connection. Returns None if DB not found."""
    if not DB_PATH.exists():
        return None
    try:
        # Use URI mode for read-only access — safe while NexusTrader is running
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")  # allow concurrent reads in WAL mode
        return conn
    except Exception as exc:
        logger.debug("DB open failed: %s", exc)
        return None


def _parse_json_field(value: Any) -> Any:
    """Parse a JSON-encoded string field; return as-is if already parsed."""
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


# ── Data access ───────────────────────────────────────────────────────────────

def read_positions() -> dict:
    """
    Read live open positions from open_positions.json.
    This file is written by PaperExecutor on every position change.
    """
    if not POS_FILE.exists():
        return {"capital": _INITIAL_CAPITAL, "peak_capital": _INITIAL_CAPITAL, "positions": []}
    try:
        raw = POS_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data
    except Exception as exc:
        logger.warning("Cannot read %s: %s", POS_FILE.name, exc)
        return {"capital": _INITIAL_CAPITAL, "peak_capital": _INITIAL_CAPITAL, "positions": []}


def read_closed_trades(
    limit:  int = 500,
    offset: int = 0,
    symbol: str | None = None,
    side:   str | None = None,
) -> list[dict]:
    """
    Read closed trades from paper_trades (SQLite authoritative source).
    Falls back to empty list if DB unavailable.
    """
    conn = _db_open()
    if conn is None:
        return []

    try:
        where_parts: list[str] = []
        params: list[Any] = []
        if symbol:
            where_parts.append("symbol = ?")
            params.append(symbol)
        if side:
            where_parts.append("side = ?")
            params.append(side.lower())

        where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

        sql = f"""
            SELECT
                id, symbol, side, regime, timeframe,
                entry_price, exit_price, stop_loss, take_profit,
                size_usdt, entry_size_usdt, exit_size_usdt,
                pnl_usdt, pnl_pct, score, exit_reason,
                models_fired, rationale, duration_s,
                opened_at, closed_at, created_at
            FROM paper_trades
            {where_sql}
            ORDER BY closed_at DESC
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])

        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        conn.close()

        # Normalise JSON string fields
        for row in rows:
            row["models_fired"] = _parse_json_field(row.get("models_fired")) or []

        return rows

    except Exception as exc:
        logger.warning("paper_trades query failed: %s", exc)
        conn.close()
        return []


def read_orders(limit: int = 200) -> list[dict]:
    """Read orders from the orders table."""
    conn = _db_open()
    if conn is None:
        return []

    try:
        sql = """
            SELECT
                id, trade_id, exchange_order_id, symbol,
                order_type, side, price, amount, filled,
                remaining, status, timestamp, created_at
            FROM orders
            ORDER BY created_at DESC
            LIMIT ?
        """
        rows = [dict(r) for r in conn.execute(sql, [limit]).fetchall()]
        conn.close()
        return rows
    except Exception as exc:
        logger.warning("orders query failed: %s", exc)
        conn.close()
        return []


def compute_summary() -> dict:
    """Compute account-level summary metrics from all data sources."""
    pos_data   = read_positions()
    capital    = float(pos_data.get("capital",      _INITIAL_CAPITAL) or _INITIAL_CAPITAL)
    peak_cap   = float(pos_data.get("peak_capital", capital)          or capital)
    open_pos   = pos_data.get("positions", []) or []

    # Fetch all trades for aggregate stats (no UI limit here)
    all_trades = read_closed_trades(limit=50_000)
    total      = len(all_trades)
    wins       = sum(1 for t in all_trades if (t.get("pnl_usdt") or 0.0) > 0)
    losses     = total - wins
    win_rate   = round(wins / total * 100, 2) if total else 0.0

    total_pnl   = sum(float(t.get("pnl_usdt") or 0.0) for t in all_trades)
    unrealized  = sum(float(p.get("unrealized_pnl") or 0.0) for p in open_pos)

    # Profit factor
    gross_profit = sum(float(t.get("pnl_usdt") or 0.0) for t in all_trades if (t.get("pnl_usdt") or 0.0) > 0)
    gross_loss   = abs(sum(float(t.get("pnl_usdt") or 0.0) for t in all_trades if (t.get("pnl_usdt") or 0.0) < 0))
    profit_factor = round(gross_profit / gross_loss, 3) if gross_loss > 0 else None

    # Drawdown
    drawdown_pct = 0.0
    if peak_cap > 0 and capital < peak_cap:
        drawdown_pct = round((peak_cap - capital) / peak_cap * 100, 4)

    return_pct = round((capital - _INITIAL_CAPITAL) / _INITIAL_CAPITAL * 100, 4)

    # Average R per trade (pnl / risk_amount)
    avg_r_values = []
    for t in all_trades:
        entry  = float(t.get("entry_price") or 0)
        sl     = float(t.get("stop_loss")   or 0)
        pnl    = float(t.get("pnl_usdt")    or 0)
        size   = float(t.get("entry_size_usdt") or t.get("size_usdt") or 0)
        if entry and sl and size:
            risk_pct = abs(entry - sl) / entry
            risk_usdt = size * risk_pct
            if risk_usdt > 0:
                avg_r_values.append(pnl / risk_usdt)
    avg_r = round(sum(avg_r_values) / len(avg_r_values), 3) if avg_r_values else None

    return {
        "capital":            round(capital, 2),
        "peak_capital":       round(peak_cap, 2),
        "initial_capital":    _INITIAL_CAPITAL,
        "return_pct":         return_pct,
        "total_pnl_usdt":     round(total_pnl, 2),
        "unrealized_pnl":     round(unrealized, 2),
        "total_trades":       total,
        "wins":               wins,
        "losses":             losses,
        "win_rate_pct":       win_rate,
        "drawdown_pct":       drawdown_pct,
        "profit_factor":      profit_factor,
        "avg_r":              avg_r,
        "open_positions":     len(open_pos),
        "timestamp":          datetime.now(timezone.utc).isoformat(),
    }


# ── HTTP handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    """Minimal HTTP request handler with CORS and JSON responses."""

    def log_message(self, fmt: str, *args: Any) -> None:
        # Suppress health-check noise; log everything else
        if args and "/api/health" in str(args[0]):
            return
        logger.info("%s — %s", self.address_string(), fmt % args)

    def _send(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(status)
        for k, v in _cors().items():
            self.send_header(k, v)
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, path: Path, status: int = 200) -> None:
        body = path.read_bytes()
        self.send_response(status)
        self.send_header("Content-Type",   "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Cache-Control",  "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:                     # preflight
        self.send_response(204)
        for k, v in _cors().items():
            self.send_header(k, v)
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/")
        qs     = parse_qs(parsed.query)

        def _qs(key: str, default: str | None = None) -> str | None:
            vals = qs.get(key)
            return vals[0] if vals else default

        try:
            if path in ("", "/", "/dashboard"):
                if HTML_FILE.exists():
                    self._send_html(HTML_FILE)
                else:
                    self._send({"error": f"orders_positions.html not found at {HTML_FILE}"}, 404)

            elif path == "/api/health":
                self._send({
                    "status":    "ok",
                    "db":        DB_PATH.exists(),
                    "positions": POS_FILE.exists(),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })

            elif path == "/api/summary":
                self._send(compute_summary())

            elif path == "/api/positions":
                self._send(read_positions())

            elif path == "/api/trades":
                trades = read_closed_trades(
                    limit  = int(_qs("limit",  "500")),
                    offset = int(_qs("offset", "0")),
                    symbol = _qs("symbol"),
                    side   = _qs("side"),
                )
                self._send({"trades": trades, "count": len(trades)})

            elif path == "/api/orders":
                orders = read_orders(limit=int(_qs("limit", "200")))
                self._send({"orders": orders, "count": len(orders)})

            else:
                self._send({"error": f"Unknown endpoint: {path}"}, 404)

        except Exception as exc:
            logger.error("Handler error: %s", exc, exc_info=True)
            self._send({"error": str(exc)}, 500)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="NexusTrader local API server for Orders & Positions dashboard"
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"Listen port (default: {DEFAULT_PORT})")
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"Listen host (default: {DEFAULT_HOST})")
    args = parser.parse_args()

    if not DB_PATH.exists():
        logger.warning("⚠  Database not found at %s", DB_PATH)
        logger.warning("   Closed trades and orders will be empty until NexusTrader runs")
    if not POS_FILE.exists():
        logger.warning("⚠  open_positions.json not found at %s", POS_FILE)
        logger.warning("   Open positions will be empty until NexusTrader creates it")

    import socket
    local_ip = socket.gethostbyname(socket.gethostname())

    server = HTTPServer((args.host, args.port), Handler)
    logger.info("=" * 60)
    logger.info("NexusTrader Dashboard")
    logger.info("  Local:   http://127.0.0.1:%d", args.port)
    logger.info("  Network: http://%s:%d  ← open this on your phone", local_ip, args.port)
    logger.info("Data directory: %s", DATA_DIR)
    logger.info("=" * 60)
    logger.info("Press Ctrl+C to stop")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Stopping server")
        server.shutdown()
    except Exception as exc:
        logger.error("Server error: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
