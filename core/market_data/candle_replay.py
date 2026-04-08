# ============================================================
# NEXUS TRADER — Candle Replay  (Phase 3 Addendum, Fix 2)
#
# Deterministic replay of 1m candle streams through the pipeline.
# ZERO PySide6 imports.
#
# Provides:
#   1. CandleFixture: serializable list of raw 1m candles
#   2. replay(): feeds a fixture through DataEngine → CandleBuilder
#      and returns all derived candles in deterministic order
#   3. Replay validation: same input → identical output guarantee
#
# Usage (in tests or audit):
#     fixture = CandleFixture.from_raw("BTC/USDT", raw_candles)
#     results1 = replay(fixture, engine, builder)
#     results2 = replay(fixture, engine, builder)
#     assert results1 == results2  # Deterministic!
# ============================================================
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from core.market_data.candle_trace import CandleSource

logger = logging.getLogger(__name__)


@dataclass
class CandleFixture:
    """
    A replayable fixture containing raw 1m candles for one or more symbols.

    Attributes
    ----------
    entries : list[dict]
        Each entry: {"symbol": str, "timeframe": str, "candles": [[ts, o, h, l, c, v], ...]}
        Entries are ordered — replay processes them in this exact sequence.
    metadata : dict
        Optional metadata (description, created_at, source_session, etc.)
    """
    entries: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    @classmethod
    def from_raw(cls, symbol: str, raw_candles: list, timeframe: str = "1m",
                 metadata: Optional[dict] = None) -> "CandleFixture":
        """Create a fixture from a single symbol's raw candle list."""
        return cls(
            entries=[{"symbol": symbol, "timeframe": timeframe, "candles": raw_candles}],
            metadata=metadata or {},
        )

    @classmethod
    def from_multi_symbol(cls, data: dict, metadata: Optional[dict] = None) -> "CandleFixture":
        """
        Create a fixture from multiple symbols.

        Parameters
        ----------
        data : dict
            {symbol: [[ts, o, h, l, c, v], ...], ...}
        """
        entries = [
            {"symbol": sym, "timeframe": "1m", "candles": candles}
            for sym, candles in sorted(data.items())  # Sort for determinism
        ]
        return cls(entries=entries, metadata=metadata or {})

    def to_json(self) -> str:
        """Serialize to JSON for persistence."""
        return json.dumps({"entries": self.entries, "metadata": self.metadata}, indent=2)

    @classmethod
    def from_json(cls, json_str: str) -> "CandleFixture":
        """Deserialize from JSON."""
        data = json.loads(json_str)
        return cls(entries=data["entries"], metadata=data.get("metadata", {}))

    def total_candles(self) -> int:
        return sum(len(e["candles"]) for e in self.entries)


@dataclass
class ReplayResult:
    """
    Captures the deterministic output of a replay run.

    All derived candles are stored in the exact order they were emitted.
    """
    derived_candles: list = field(default_factory=list)
    # Each entry: {"symbol": str, "timeframe": str, "candle": dict, "sequence": int}
    publication_sequence: list = field(default_factory=list)
    # Ordered list of (topic, symbol, tf, timestamp) for sequence comparison
    candles_ingested: int = 0

    def add_candle(self, symbol: str, timeframe: str, candle: dict) -> None:
        seq = len(self.derived_candles)
        entry = {
            "symbol": symbol,
            "timeframe": timeframe,
            "candle": candle,
            "sequence": seq,
        }
        self.derived_candles.append(entry)
        self.publication_sequence.append(
            (symbol, timeframe, candle.get("timestamp", 0))
        )

    def matches(self, other: "ReplayResult") -> bool:
        """
        Check if two replay results are deterministically identical.
        Compares publication sequence and candle values.
        """
        if len(self.derived_candles) != len(other.derived_candles):
            return False
        if self.publication_sequence != other.publication_sequence:
            return False
        for a, b in zip(self.derived_candles, other.derived_candles):
            if a["symbol"] != b["symbol"]:
                return False
            if a["timeframe"] != b["timeframe"]:
                return False
            ac, bc = a["candle"], b["candle"]
            for key in ("timestamp", "open", "high", "low", "close", "volume"):
                if ac.get(key) != bc.get(key):
                    return False
        return True

    def diff(self, other: "ReplayResult") -> list[str]:
        """Return list of differences between two replay results."""
        diffs = []
        if len(self.derived_candles) != len(other.derived_candles):
            diffs.append(
                f"Candle count mismatch: {len(self.derived_candles)} vs {len(other.derived_candles)}"
            )
        if self.publication_sequence != other.publication_sequence:
            diffs.append("Publication sequence differs")
            min_len = min(len(self.publication_sequence), len(other.publication_sequence))
            for i in range(min_len):
                if self.publication_sequence[i] != other.publication_sequence[i]:
                    diffs.append(f"  Seq[{i}]: {self.publication_sequence[i]} vs {other.publication_sequence[i]}")
                    break
        return diffs


def replay(
    fixture: CandleFixture,
    data_engine,
    candle_builder,
    source: CandleSource = CandleSource.REPLAY,
) -> ReplayResult:
    """
    Replay a candle fixture through the data pipeline deterministically.

    Parameters
    ----------
    fixture : CandleFixture
        The fixture to replay
    data_engine : DataEngine
        Must be started. Buffers will be populated.
    candle_builder : CandleBuilder
        Must be started. Derived candles will be emitted.
    source : CandleSource
        Source tag for traceability (default: REPLAY)

    Returns
    -------
    ReplayResult : all derived candles in emission order
    """
    result = ReplayResult()

    # Temporarily replace the builder's on_candle callback to capture output
    original_callback = candle_builder._on_candle

    def capture_callback(symbol: str, tf: str, candle: dict) -> None:
        result.add_candle(symbol, tf, candle)
        # Also call original if present
        if original_callback:
            original_callback(symbol, tf, candle)

    candle_builder._on_candle = capture_callback

    try:
        for entry in fixture.entries:
            symbol = entry["symbol"]
            tf = entry["timeframe"]
            candles = entry["candles"]
            count = data_engine.ingest_candles(symbol, tf, candles, source=source)
            result.candles_ingested += count
    finally:
        # Restore original callback
        candle_builder._on_candle = original_callback

    return result
