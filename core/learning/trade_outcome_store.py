# ============================================================
# NEXUS TRADER — Trade Outcome Store (Level-2 Learning)
#
# Persists enriched trade records to a JSONL file.
# Each record captures everything needed for richer analysis:
# per-model×regime, per-model×asset, score calibration,
# entry timing, exit efficiency, and realized R-multiple.
#
# The store is append-only (JSONL) so it survives restarts
# and can be replayed for analytics.  It does NOT store
# duplicates — each trade has a unique ID derived from
# symbol + opened_at.
# ============================================================
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_STORE_FILE = Path(__file__).parent.parent.parent / "data" / "trade_outcomes.jsonl"


@dataclass
class EnrichedTrade:
    """
    Full trade record capturing every dimension needed for Level-2 learning.

    All numeric fields are float so JSON round-trips cleanly.
    Fields that are unavailable should be None — the learning system
    skips cells with None values rather than treating them as 0.
    """
    # ── Identity ─────────────────────────────────────────────
    trade_id:           str        # "{symbol}_{opened_at_iso}"
    symbol:             str        # "BTC/USDT"
    side:               str        # "buy" | "sell"

    # ── Timing ───────────────────────────────────────────────
    opened_at:          str        # ISO-8601
    closed_at:          str        # ISO-8601
    duration_s:         float      # seconds

    # ── Regime context ───────────────────────────────────────
    regime:             str        # dominant regime label at entry
    regime_confidence:  Optional[float] = None  # top HMM probability 0–1

    # ── Model firing ─────────────────────────────────────────
    models_fired:       list[str]  = field(default_factory=list)
    confluence_score:   float      = 0.0   # final score 0–1 at entry

    # ── Entry / Exit prices ───────────────────────────────────
    entry_price:        float      = 0.0
    exit_price:         float      = 0.0
    entry_expected:     Optional[float] = None   # OrderCandidate.entry_price (pre-slippage)
    stop_loss:          float      = 0.0
    take_profit:        float      = 0.0
    position_size_usdt: float      = 0.0

    # ── Risk geometry ─────────────────────────────────────────
    expected_rr:        Optional[float] = None   # (TP-entry)/(entry-SL) at trade open
    expected_value:     Optional[float] = None   # EV from RiskGate at entry

    # ── Realized outcome ─────────────────────────────────────
    realized_pnl_usdt:  float      = 0.0
    realized_pnl_pct:   float      = 0.0
    realized_r_multiple: Optional[float] = None  # P&L / initial risk
    slippage_pct:       Optional[float] = None   # |entry_fill - expected| / expected * 100
    exit_reason:        str        = ""          # "take_profit" | "stop_loss" | "manual_close" | "time_exit"

    # ── Bookkeeping ───────────────────────────────────────────
    won:                bool       = False       # pnl > 0
    recorded_at:        str        = ""          # wall-clock when recorded

    @classmethod
    def from_trade_dict(cls, trade: dict) -> "EnrichedTrade":
        """
        Build an EnrichedTrade from the dict that PaperExecutor._close_position()
        produces.  All missing optional fields are safely handled.
        """
        symbol     = trade.get("symbol", "?")
        opened_at  = trade.get("opened_at", "")
        trade_id   = f"{symbol}_{opened_at}"

        entry      = float(trade.get("entry_price", 0.0))
        sl         = float(trade.get("stop_loss",   0.0))
        tp         = float(trade.get("take_profit", 0.0))
        side       = trade.get("side", "buy")
        pnl_usdt   = float(trade.get("pnl_usdt", 0.0))
        pnl_pct    = float(trade.get("pnl_pct",   0.0))
        size       = float(trade.get("size_usdt",  0.0))

        # Expected R:R from configured SL/TP
        expected_rr = None
        if entry > 0 and sl > 0 and tp > 0:
            risk   = (entry - sl) if side == "buy" else (sl - entry)
            reward = (tp - entry) if side == "buy" else (entry - tp)
            if risk > 0:
                expected_rr = round(reward / risk, 4)

        # Realized R-multiple: P&L / initial risk in USDT
        realized_r = None
        if entry > 0 and sl > 0 and size > 0:
            risk_usdt = abs(entry - sl) / entry * size
            if risk_usdt > 0:
                realized_r = round(pnl_usdt / risk_usdt, 4)

        # Slippage vs expected entry
        expected_entry = trade.get("entry_expected")
        slippage       = None
        if expected_entry and float(expected_entry) > 0 and entry > 0:
            slippage = round(abs(entry - float(expected_entry)) / float(expected_entry) * 100, 6)

        return cls(
            trade_id           = trade_id,
            symbol             = symbol,
            side               = side,
            opened_at          = opened_at,
            closed_at          = trade.get("closed_at", ""),
            duration_s         = float(trade.get("duration_s", 0)),
            regime             = (trade.get("regime") or "unknown").lower(),
            regime_confidence  = trade.get("regime_confidence"),
            models_fired       = list(trade.get("models_fired") or []),
            confluence_score   = float(trade.get("score", 0.0)),
            entry_price        = entry,
            exit_price         = float(trade.get("exit_price", 0.0)),
            entry_expected     = float(expected_entry) if expected_entry else None,
            stop_loss          = sl,
            take_profit        = tp,
            position_size_usdt = size,
            expected_rr        = expected_rr,
            expected_value     = trade.get("expected_value"),
            realized_pnl_usdt  = round(pnl_usdt, 4),
            realized_pnl_pct   = round(pnl_pct, 4),
            realized_r_multiple = realized_r,
            slippage_pct       = slippage,
            exit_reason        = trade.get("exit_reason", ""),
            won                = pnl_pct > 0,
            recorded_at        = datetime.now(timezone.utc).isoformat(),
        )

    def to_dict(self) -> dict:
        return asdict(self)


class TradeOutcomeStore:
    """
    Append-only JSONL store for enriched trade records.

    Thread-safe.  Each flush writes one JSON line.
    Duplicate detection uses trade_id; duplicates are silently dropped.
    """

    def __init__(self, path: Path = _STORE_FILE):
        self._path    = path
        self._lock    = threading.Lock()
        self._seen:   set[str] = set()
        self._trades: list[EnrichedTrade] = []
        self._load()

    # ── Public API ──────────────────────────────────────────────────────────

    def record(self, trade: dict) -> Optional[EnrichedTrade]:
        """
        Build an EnrichedTrade from a raw trade dict and append it to the store.
        Returns the enriched record, or None if it was a duplicate.
        """
        try:
            enriched = EnrichedTrade.from_trade_dict(trade)
            with self._lock:
                if enriched.trade_id in self._seen:
                    return None
                self._seen.add(enriched.trade_id)
                self._trades.append(enriched)
            self._append_to_disk(enriched)
            return enriched
        except Exception as exc:
            logger.warning("TradeOutcomeStore.record() failed (non-fatal): %s", exc)
            return None

    def all_trades(self) -> list[EnrichedTrade]:
        """Return a snapshot of all stored enriched trades."""
        with self._lock:
            return list(self._trades)

    def trades_for_model(self, model: str) -> list[EnrichedTrade]:
        return [t for t in self.all_trades() if model in t.models_fired]

    def trades_for_regime(self, regime: str) -> list[EnrichedTrade]:
        return [t for t in self.all_trades() if t.regime == regime.lower()]

    def trades_for_asset(self, symbol: str) -> list[EnrichedTrade]:
        return [t for t in self.all_trades() if t.symbol == symbol]

    def trades_for_model_regime(self, model: str, regime: str) -> list[EnrichedTrade]:
        return [t for t in self.all_trades()
                if model in t.models_fired and t.regime == regime.lower()]

    def trades_for_model_asset(self, model: str, symbol: str) -> list[EnrichedTrade]:
        return [t for t in self.all_trades()
                if model in t.models_fired and t.symbol == symbol]

    def __len__(self) -> int:
        with self._lock:
            return len(self._trades)

    # ── Internal ────────────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            if not self._path.exists():
                return
            with open(self._path, "r") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    data = json.loads(line)
                    et = EnrichedTrade(**data)
                    if et.trade_id not in self._seen:
                        self._seen.add(et.trade_id)
                        self._trades.append(et)
            logger.debug("TradeOutcomeStore: loaded %d records from disk", len(self._trades))
        except Exception as exc:
            logger.warning("TradeOutcomeStore: load failed (starting fresh): %s", exc)

    def _append_to_disk(self, et: EnrichedTrade) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "a") as fh:
                fh.write(json.dumps(et.to_dict()) + "\n")
        except Exception as exc:
            logger.debug("TradeOutcomeStore: disk write failed (non-fatal): %s", exc)


# ── Module singleton ─────────────────────────────────────────────────────────
_outcome_store = TradeOutcomeStore()


def get_outcome_store() -> TradeOutcomeStore:
    """Return the module-level trade outcome store singleton."""
    return _outcome_store
