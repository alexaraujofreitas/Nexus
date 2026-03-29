"""
research/engine/data_registry.py
=================================
DataRegistry — scans backtest_data/ and maintains a lightweight metadata
catalogue of every parquet file available to the Research Lab.

Responsibilities
----------------
- Enumerate all symbol/timeframe combinations present in DATA_DIR
- Record date coverage (first_date, last_date), row count, file size, hash
- Persist catalogue to research/engine/data_registry.json
- Provide query helpers used by DataManager and the UI panels

Design rules
------------
- No Qt imports — this module is pure Python
- Thread-safe reads (catalogue is immutable after build())
- Graceful: missing files → status "missing", corrupt → status "error"
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

import pandas as pd

ROOT     = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT / "backtest_data"
_REGISTRY_PATH = ROOT / "research" / "engine" / "data_registry.json"

logger = logging.getLogger(__name__)

# ── Supported symbols and timeframes ─────────────────────────────────────────
SUPPORTED_SYMBOLS = ["BTC/USDT", "SOL/USDT", "ETH/USDT", "XRP/USDT", "BNB/USDT"]
REQUIRED_TFS      = ["30m", "1h", "4h"]   # must all be present for a full run
ALL_TFS           = ["5m", "15m", "30m", "1h", "4h"]

def _slug(symbol: str) -> str:
    """'BTC/USDT' → 'BTC_USDT'"""
    return symbol.replace("/", "_")

def _fingerprint(path: Path) -> str:
    """First-64 KB SHA-256 hex digest (fast, stable)."""
    with open(path, "rb") as f:
        return hashlib.sha256(f.read(65536)).hexdigest()[:16]


# ── Data record ───────────────────────────────────────────────────────────────

@dataclass
class FileRecord:
    """Metadata for one parquet file (one symbol × one timeframe)."""
    symbol:     str
    timeframe:  str
    path:       str          # relative to DATA_DIR
    status:     str          # "ok" | "missing" | "error"
    rows:       int  = 0
    size_bytes: int  = 0
    first_date: str  = ""   # ISO date string "2022-03-22"
    last_date:  str  = ""
    hash:       str  = ""
    updated_at: str  = ""   # ISO datetime when record was last refreshed

    # Computed property helpers
    def ok(self) -> bool:
        return self.status == "ok"

    def covers(self, start: str, end: str) -> bool:
        """Return True if the file covers [start, end] completely."""
        if not self.ok() or not self.first_date or not self.last_date:
            return False
        return self.first_date <= start and self.last_date >= end

    def coverage_years(self) -> float:
        if not self.ok() or not self.first_date or not self.last_date:
            return 0.0
        try:
            d0 = pd.Timestamp(self.first_date)
            d1 = pd.Timestamp(self.last_date)
            return (d1 - d0).days / 365.25
        except Exception:
            return 0.0


@dataclass
class SymbolRecord:
    """All timeframes for one symbol."""
    symbol:  str
    files:   dict  # timeframe → FileRecord

    def required_ok(self) -> bool:
        """All REQUIRED_TFS present and ok."""
        return all(
            self.files.get(tf, FileRecord(self.symbol, tf, "", "missing")).ok()
            for tf in REQUIRED_TFS
        )

    def status_summary(self) -> str:
        if self.required_ok():
            recs = [self.files[tf] for tf in REQUIRED_TFS if tf in self.files]
            if recs:
                return f"OK  ({recs[0].first_date} → {recs[0].last_date})"
            return "OK"
        missing = [tf for tf in REQUIRED_TFS
                   if not self.files.get(tf, FileRecord("", tf, "", "missing")).ok()]
        return f"Missing: {', '.join(missing)}"


# ── Registry class ────────────────────────────────────────────────────────────

class DataRegistry:
    """
    Scans DATA_DIR and builds a symbol×timeframe metadata catalogue.

    Usage
    -----
    reg = DataRegistry()
    reg.build()                            # scan disk, update catalogue
    reg.save()                             # persist to JSON
    rec = reg.get("BTC/USDT", "30m")      # FileRecord | None
    ok  = reg.symbol_ready("BTC/USDT")    # bool — all required TFs present
    """

    def __init__(self):
        self._records: dict[str, SymbolRecord] = {}   # symbol → SymbolRecord

    # ── Build / scan ─────────────────────────────────────────────────────────

    def build(self, symbols: list[str] | None = None, progress_cb=None) -> None:
        """
        Scan DATA_DIR for all known symbols and timeframes.
        Existing cached records are refreshed only if the file hash changed.

        progress_cb: optional callable(symbol: str, done: int, total: int)
        """
        targets = symbols or SUPPORTED_SYMBOLS
        for i, sym in enumerate(targets):
            slug = _slug(sym)
            tf_records: dict[str, FileRecord] = {}

            for tf in ALL_TFS:
                fname = f"{slug}_{tf}.parquet"
                fpath = DATA_DIR / fname
                rec   = self._scan_file(sym, tf, fpath)
                tf_records[tf] = rec

            self._records[sym] = SymbolRecord(symbol=sym, files=tf_records)
            if progress_cb:
                progress_cb(sym, i + 1, len(targets))

        logger.info("DataRegistry.build(): %d symbols scanned", len(self._records))

    def _scan_file(self, symbol: str, timeframe: str, path: Path) -> FileRecord:
        import datetime
        now = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
        rel = str(path.relative_to(DATA_DIR)) if path.is_relative_to(DATA_DIR) else str(path)

        if not path.exists():
            return FileRecord(symbol, timeframe, rel, "missing", updated_at=now)

        try:
            size  = path.stat().st_size
            h     = _fingerprint(path)

            # Check if we already have an up-to-date record
            existing = self._records.get(symbol, SymbolRecord(symbol, {})).files.get(timeframe)
            if existing and existing.hash == h and existing.ok():
                # Refresh timestamp only
                existing.updated_at = now
                return existing

            df = pd.read_parquet(path, columns=["open", "close"])
            if df.index.tz is None:
                df.index = df.index.tz_localize("UTC")

            first = df.index[0].strftime("%Y-%m-%d")
            last  = df.index[-1].strftime("%Y-%m-%d")

            return FileRecord(
                symbol    = symbol,
                timeframe = timeframe,
                path      = rel,
                status    = "ok",
                rows      = len(df),
                size_bytes= size,
                first_date= first,
                last_date = last,
                hash      = h,
                updated_at= now,
            )
        except Exception as exc:
            logger.warning("DataRegistry: error scanning %s: %s", path, exc)
            return FileRecord(symbol, timeframe, rel, "error", updated_at=now)

    # ── Persistence ───────────────────────────────────────────────────────────

    def save(self) -> None:
        """Persist catalogue to data_registry.json."""
        try:
            payload = {}
            for sym, srec in self._records.items():
                payload[sym] = {
                    tf: asdict(frec)
                    for tf, frec in srec.files.items()
                }
            _REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
            _REGISTRY_PATH.write_text(json.dumps(payload, indent=2))
            logger.info("DataRegistry saved → %s", _REGISTRY_PATH)
        except Exception as exc:
            logger.warning("DataRegistry.save() failed: %s", exc)

    def load(self) -> bool:
        """Load catalogue from JSON. Returns True on success."""
        if not _REGISTRY_PATH.exists():
            return False
        try:
            raw = json.loads(_REGISTRY_PATH.read_text())
            for sym, tfs in raw.items():
                tf_records = {}
                for tf, d in tfs.items():
                    tf_records[tf] = FileRecord(**d)
                self._records[sym] = SymbolRecord(symbol=sym, files=tf_records)
            logger.info("DataRegistry loaded %d symbols from cache", len(self._records))
            return True
        except Exception as exc:
            logger.warning("DataRegistry.load() failed: %s", exc)
            return False

    # ── Query API ─────────────────────────────────────────────────────────────

    def get(self, symbol: str, timeframe: str) -> Optional[FileRecord]:
        """Return FileRecord for (symbol, timeframe), or None if unknown."""
        srec = self._records.get(symbol)
        if not srec:
            return None
        return srec.files.get(timeframe)

    def symbol_record(self, symbol: str) -> Optional[SymbolRecord]:
        return self._records.get(symbol)

    def symbol_ready(self, symbol: str) -> bool:
        """True if all REQUIRED_TFS are present and ok for this symbol."""
        srec = self._records.get(symbol)
        return srec is not None and srec.required_ok()

    def all_symbols(self) -> list[str]:
        """All symbols with at least one file scanned."""
        return list(self._records.keys())

    def available_symbols(self) -> list[str]:
        """Symbols where all REQUIRED_TFS are ok."""
        return [s for s in self._records if self.symbol_ready(s)]

    def coverage_for(self, symbol: str) -> tuple[str, str]:
        """Return (first_date, last_date) from the 30m file for this symbol."""
        rec = self.get(symbol, "30m")
        if rec and rec.ok():
            return rec.first_date, rec.last_date
        return "", ""

    def validate_period(self, symbols: list[str], date_start: str, date_end: str) -> list[str]:
        """
        Return a list of warning strings for symbols whose data does not fully
        cover [date_start, date_end] in all REQUIRED_TFS.
        Empty list → all good.
        """
        issues = []
        for sym in symbols:
            srec = self._records.get(sym)
            if not srec:
                issues.append(f"{sym}: not in registry — run Check Data first")
                continue
            for tf in REQUIRED_TFS:
                frec = srec.files.get(tf)
                if not frec or not frec.ok():
                    issues.append(f"{sym} {tf}: missing or error")
                elif not frec.covers(date_start, date_end):
                    issues.append(
                        f"{sym} {tf}: covers {frec.first_date}→{frec.last_date}, "
                        f"but run needs {date_start}→{date_end}"
                    )
        return issues

    def summary_table(self) -> list[dict]:
        """
        Return a list of dicts for the UI data-status table.
        Keys: symbol, status, first_date, last_date, rows_30m, size_mb
        """
        rows = []
        for sym in SUPPORTED_SYMBOLS:
            srec = self._records.get(sym)
            if srec is None:
                rows.append({
                    "symbol": sym, "status": "Not scanned",
                    "first_date": "—", "last_date": "—",
                    "rows_30m": 0, "size_mb": 0.0,
                })
                continue
            rec30 = srec.files.get("30m", FileRecord(sym, "30m", "", "missing"))
            all_req_ok = srec.required_ok()
            rows.append({
                "symbol":     sym,
                "status":     "Ready" if all_req_ok else srec.status_summary(),
                "first_date": rec30.first_date or "—",
                "last_date":  rec30.last_date  or "—",
                "rows_30m":   rec30.rows,
                "size_mb":    round(sum(
                    srec.files.get(tf, FileRecord(sym, tf, "", "missing")).size_bytes
                    for tf in REQUIRED_TFS
                ) / 1_048_576, 1),
            })
        return rows
