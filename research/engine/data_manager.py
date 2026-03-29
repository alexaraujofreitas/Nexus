"""
research/engine/data_manager.py
================================
DataManager — higher-level controller that wraps DataRegistry and handles
the fetch → build pipeline for adding new assets to the Research Lab.

Responsibilities
----------------
1. check(symbols, date_start, date_end) → CheckResult
   Runs DataRegistry.validate_period() and returns a structured result
   the UI can render immediately.

2. validate_exchange_symbol(symbol) → (bool, str)
   Hits Bybit REST to confirm the symbol exists and is active.

3. fetch_symbol(symbol, timeframes, date_start, date_end, progress_cb)
   Downloads OHLCV from Bybit via ccxt and saves parquets.
   Calls progress_cb(pct: float, msg: str) periodically.

4. refresh_registry(progress_cb)
   Calls DataRegistry.build() + save(). Used by the UI "Check Data" button.

Design rules
------------
- No Qt imports — pure Python so tests can run headless
- All network calls use ccxt REST (not WS) via get_exchange()
- Errors are caught and returned as messages, never raised to caller
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "backtest_data"

from research.engine.data_registry import (
    DataRegistry, REQUIRED_TFS, SUPPORTED_SYMBOLS, _slug,
)


# ── Check result ──────────────────────────────────────────────────────────────

@dataclass
class CheckResult:
    """Result of a data availability check for a set of symbols + period."""
    ok:       bool            # True if all symbols fully covered
    ready:    list[str] = field(default_factory=list)   # symbols that are ready
    missing:  list[str] = field(default_factory=list)   # symbols with missing files
    partial:  list[str] = field(default_factory=list)   # files present but wrong dates
    issues:   list[str] = field(default_factory=list)   # human-readable issue strings

    def summary(self) -> str:
        if self.ok:
            return f"All {len(self.ready)} selected symbols ready."
        parts = []
        if self.missing:
            parts.append(f"{len(self.missing)} missing")
        if self.partial:
            parts.append(f"{len(self.partial)} partial coverage")
        return "Issues found: " + ", ".join(parts) + ". See details below."


# ── DataManager ───────────────────────────────────────────────────────────────

class DataManager:
    """
    Orchestrates data checks and fetches for the Research Lab.

    Usage
    -----
    dm = DataManager()
    dm.refresh_registry()                        # scan disk
    result = dm.check(["BTC/USDT"], "2022-03-22", "2026-03-21")
    ok, msg = dm.validate_exchange_symbol("DOGE/USDT")
    dm.fetch_symbol("DOGE/USDT", progress_cb=lambda p, m: print(p, m))
    """

    def __init__(self, registry: DataRegistry | None = None):
        self._reg = registry or DataRegistry()
        self._reg_loaded = False

    # ── Registry ─────────────────────────────────────────────────────────────

    def registry(self) -> DataRegistry:
        return self._reg

    def refresh_registry(self, progress_cb: Callable | None = None) -> None:
        """Scan DATA_DIR and persist updated registry."""
        self._reg.build(progress_cb=progress_cb)
        self._reg.save()
        self._reg_loaded = True

    def ensure_registry(self) -> None:
        """Load from JSON cache if not yet done; fall back to full scan."""
        if not self._reg_loaded:
            if not self._reg.load():
                self._reg.build()
                self._reg.save()
            self._reg_loaded = True

    # ── Check ─────────────────────────────────────────────────────────────────

    def check(
        self,
        symbols:    list[str],
        date_start: str,
        date_end:   str,
    ) -> CheckResult:
        """
        Check whether all (symbol, required_tf) combos exist and cover [start, end].
        Does NOT hit the network — purely local file check.
        """
        self.ensure_registry()
        issues = self._reg.validate_period(symbols, date_start, date_end)

        ready:   list[str] = []
        missing: list[str] = []
        partial: list[str] = []

        for sym in symbols:
            srec = self._reg.symbol_record(sym)
            if srec is None or not srec.required_ok():
                missing.append(sym)
            else:
                sym_issues = [i for i in issues if i.startswith(sym)]
                if sym_issues:
                    partial.append(sym)
                else:
                    ready.append(sym)

        return CheckResult(
            ok      = len(issues) == 0,
            ready   = ready,
            missing = missing,
            partial = partial,
            issues  = issues,
        )

    # ── Exchange validation ───────────────────────────────────────────────────

    def validate_exchange_symbol(self, symbol: str) -> tuple[bool, str]:
        """
        Hit Bybit REST to confirm symbol exists and is active.
        Returns (True, "") on success, (False, reason) on failure.
        Requires ccxt to be installed and Bybit accessible.
        """
        try:
            from core.market_data.exchange_manager import get_exchange
            exchange = get_exchange()
            markets  = exchange.load_markets()
            if symbol not in markets:
                return False, f"{symbol} not found on Bybit"
            mkt = markets[symbol]
            if not mkt.get("active", True):
                return False, f"{symbol} is not active on Bybit"
            return True, ""
        except Exception as exc:
            return False, f"Exchange error: {exc}"

    # ── Fetch ─────────────────────────────────────────────────────────────────

    def fetch_symbol(
        self,
        symbol:     str,
        timeframes: list[str] | None = None,
        date_start: str = "2022-01-01",
        date_end:   str = "2026-03-21",
        progress_cb: Callable[[float, str], None] | None = None,
    ) -> tuple[bool, str]:
        """
        Download OHLCV candles for (symbol, timeframes) from Bybit and save
        as parquet files in DATA_DIR.

        Returns (True, "") on success, (False, error_message) on failure.
        progress_cb(pct: float [0-1], msg: str) — called periodically.
        """
        tfs = timeframes or REQUIRED_TFS
        slug = _slug(symbol)
        total_steps = len(tfs)

        def _cb(msg: str, pct: float):
            if progress_cb:
                progress_cb(pct, msg)

        try:
            import ccxt
            import pandas as pd
            import numpy as np

            from core.market_data.exchange_manager import get_exchange
            exchange = get_exchange()

            for step_i, tf in enumerate(tfs):
                base_pct = step_i / total_steps
                _cb(f"Fetching {symbol} {tf}…", base_pct)

                ohlcv = self._fetch_ohlcv_all(
                    exchange, symbol, tf, date_start, date_end,
                    progress_cb=lambda pct, msg: _cb(
                        msg, base_pct + pct / total_steps
                    ),
                )
                if ohlcv is None or len(ohlcv) == 0:
                    return False, f"No data returned for {symbol} {tf}"

                df = pd.DataFrame(
                    ohlcv,
                    columns=["timestamp", "open", "high", "low", "close", "volume"],
                )
                df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
                df = df.set_index("timestamp").sort_index()
                df = df[~df.index.duplicated(keep="last")]

                out_path = DATA_DIR / f"{slug}_{tf}.parquet"
                df.to_parquet(out_path)
                _cb(f"Saved {slug}_{tf}.parquet ({len(df):,} rows)", (step_i + 1) / total_steps)
                logger.info("Fetched %s %s → %s rows → %s", symbol, tf, len(df), out_path)

            # Refresh registry for this symbol
            self._reg.build(symbols=[symbol])
            self._reg.save()
            return True, ""

        except Exception as exc:
            logger.exception("DataManager.fetch_symbol(%s) failed", symbol)
            return False, str(exc)

    def _fetch_ohlcv_all(
        self,
        exchange,
        symbol:   str,
        tf:       str,
        date_start: str,
        date_end:   str,
        progress_cb: Callable | None = None,
    ) -> list:
        """Paginate ccxt fetch_ohlcv until date_end is reached."""
        import time as _time
        import pandas as pd

        since    = int(pd.Timestamp(date_start, tz="UTC").timestamp() * 1000)
        until    = int(pd.Timestamp(date_end,   tz="UTC").timestamp() * 1000)
        all_rows = []
        limit    = 1000

        while True:
            batch = exchange.fetch_ohlcv(symbol, tf, since=since, limit=limit)
            if not batch:
                break
            all_rows.extend(batch)
            last_ts = batch[-1][0]
            if progress_cb:
                span   = until - int(pd.Timestamp(date_start, tz="UTC").timestamp() * 1000)
                done   = last_ts - int(pd.Timestamp(date_start, tz="UTC").timestamp() * 1000)
                progress_cb(min(done / span, 0.99), f"  …{len(all_rows):,} candles")
            if last_ts >= until or len(batch) < limit:
                break
            since = last_ts + 1
            _time.sleep(0.25)   # respect rate limit

        return [r for r in all_rows if r[0] <= until]

    # ── Add-asset flow (validate + fetch + update registry) ──────────────────

    def add_asset(
        self,
        symbol:     str,
        date_start: str = "2022-01-01",
        date_end:   str = "2026-03-21",
        progress_cb: Callable[[float, str], None] | None = None,
    ) -> tuple[bool, str]:
        """
        Full add-asset pipeline:
          1. Validate symbol exists on exchange
          2. Fetch REQUIRED_TFS
          3. Rebuild registry for new symbol

        Returns (True, "") on success, (False, reason) on any failure.
        """
        def _cb(pct: float, msg: str):
            if progress_cb:
                progress_cb(pct, msg)

        _cb(0.0, f"Validating {symbol} on Bybit…")
        ok, reason = self.validate_exchange_symbol(symbol)
        if not ok:
            return False, reason

        _cb(0.05, f"{symbol} confirmed active — fetching data…")
        ok, reason = self.fetch_symbol(
            symbol,
            timeframes  = REQUIRED_TFS,
            date_start  = date_start,
            date_end    = date_end,
            progress_cb = lambda pct, msg: _cb(0.05 + pct * 0.90, msg),
        )
        if not ok:
            return False, reason

        _cb(0.98, "Updating registry…")
        self._reg.build(symbols=[symbol])
        self._reg.save()
        _cb(1.0, f"{symbol} added successfully.")
        return True, ""
