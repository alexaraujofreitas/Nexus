# ============================================================
# NEXUS TRADER — LTF (Lower-Timeframe) Scan Worker
#
# Runs on a 15-minute cycle (separate from the 1H HTF scan).
# Evaluates CREATED staged candidates using 15m closed-candle
# data to confirm, void, or leave them for the next cycle.
#
# CRITICAL RULES:
#   1. This worker NEVER generates signals. Signal generation
#      is the exclusive domain of the 1H ScanWorker.
#   2. This worker NEVER submits trades. It only transitions
#      candidates to CONFIRMED (or VOIDED).
#   3. Execution of CONFIRMED candidates happens in the main
#      thread via the AssetScanner signal pathway.
# ============================================================
from __future__ import annotations

import concurrent.futures
import logging
import time
from typing import Optional

import pandas as pd
from PySide6.QtCore import QThread, Signal

from core.scanning.closed_candle_guard import enforce_closed_candles
from core.scanning.ltf_confirmation import (
    LTFConfirmationConfig,
    evaluate_confirmation,
)
from core.scanning.candidate_store import (
    CandidateState,
    StagedCandidate,
    CandidateStore,
    get_candidate_store,
)

logger = logging.getLogger(__name__)


class LTFScanWorker(QThread):
    """
    Background thread that evaluates all CREATED staged candidates
    against 15m closed-candle data.

    Emits:
        ltf_complete(list[dict])  — list of newly CONFIRMED candidate dicts
                                    (ready for execution pathway)
        ltf_error(str)            — fatal error description
    """

    ltf_complete = Signal(list)   # list of confirmed candidate raw_candidate_dicts
    ltf_error = Signal(str)

    def __init__(
        self,
        exchange,                     # ccxt exchange instance
        store: Optional[CandidateStore] = None,
        cfg: Optional[LTFConfirmationConfig] = None,
        parent=None,
    ):
        super().__init__(parent)
        self._exchange = exchange
        self._store = store or get_candidate_store()
        self._cfg = cfg or LTFConfirmationConfig.from_settings()

    def run(self):
        try:
            # ── Step 1: Expire stale candidates ────────────────────
            expired_ids = self._store.expire_stale()
            if expired_ids:
                logger.info(
                    "LTFScanWorker: expired %d stale candidate(s): %s",
                    len(expired_ids), expired_ids,
                )

            # ── Step 2: Clean up old terminal candidates ───────────
            cleaned = self._store.cleanup_terminal()
            if cleaned:
                logger.debug("LTFScanWorker: cleaned %d terminal candidate(s)", cleaned)

            # ── Step 3: Get all CREATED candidates ─────────────────
            created = self._store.get_created()
            if not created:
                logger.debug("LTFScanWorker: no CREATED candidates to evaluate")
                self.ltf_complete.emit([])
                return

            logger.info(
                "LTFScanWorker: evaluating %d CREATED candidate(s) — %s",
                len(created),
                [f"{c.symbol} {c.side}" for c in created],
            )

            # ── Step 4: Fetch 15m OHLCV for each unique symbol ────
            # Multiple candidates may share a symbol (different conditions),
            # so we fetch once per symbol and reuse.
            symbols_needed = list({c.symbol for c in created})
            ohlcv_cache = self._fetch_ltf_ohlcv(symbols_needed)

            # ── Step 5: Evaluate each candidate ────────────────────
            newly_confirmed: list[dict] = []

            for candidate in created:
                try:
                    result_dict = self._evaluate_one(candidate, ohlcv_cache)
                    if result_dict is not None:
                        newly_confirmed.append(result_dict)
                except Exception as exc:
                    logger.error(
                        "LTFScanWorker: error evaluating %s: %s",
                        candidate.candidate_id, exc, exc_info=True,
                    )

            logger.info(
                "LTFScanWorker: cycle complete — %d confirmed, %d expired, "
                "%d still CREATED",
                len(newly_confirmed), len(expired_ids),
                len(self._store.get_created()),
            )

            self.ltf_complete.emit(newly_confirmed)

        except Exception as exc:
            logger.error("LTFScanWorker fatal error: %s", exc, exc_info=True)
            self.ltf_error.emit(str(exc))

    def _fetch_ltf_ohlcv(self, symbols: list[str]) -> dict[str, pd.DataFrame]:
        """Fetch 15m OHLCV for all symbols concurrently. Returns {symbol: DataFrame}."""
        cache: dict[str, pd.DataFrame] = {}
        ltf_tf = self._cfg.timeframe
        ltf_limit = self._cfg.ohlcv_limit

        def _fetch_one(sym: str) -> tuple[str, list]:
            try:
                raw = self._exchange.fetch_ohlcv(sym, ltf_tf, limit=ltf_limit)
                raw, _dropped = enforce_closed_candles(raw, ltf_tf, log_symbol=f"{sym}/LTF")
                return sym, raw
            except Exception as exc:
                logger.warning("LTFScanWorker: fetch failed for %s: %s", sym, exc)
                return sym, []

        # IMPORTANT: Do NOT use `with ThreadPoolExecutor` here.
        # shutdown(wait=True) in __exit__ blocks forever if any fetch hangs.
        pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=min(len(symbols), 8)
        )
        try:
            futures = {pool.submit(_fetch_one, sym): sym for sym in symbols}
            for fut in concurrent.futures.as_completed(futures, timeout=30):
                try:
                    sym, raw = fut.result()
                    if raw and len(raw) >= 10:
                        df = pd.DataFrame(
                            raw,
                            columns=["timestamp", "open", "high", "low", "close", "volume"],
                        )
                        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
                        df = df.set_index("timestamp").astype(float)
                        cache[sym] = df
                    else:
                        logger.warning(
                            "LTFScanWorker: insufficient 15m data for %s (%d bars)",
                            sym, len(raw) if raw else 0,
                        )
                except Exception as exc:
                    sym = futures[fut]
                    logger.warning("LTFScanWorker: prefetch error for %s: %s", sym, exc)
        except concurrent.futures.TimeoutError:
            logger.warning("LTFScanWorker: 15m OHLCV prefetch timed out")
        except Exception as exc:
            logger.warning("LTFScanWorker: 15m OHLCV prefetch failed: %s", exc)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        return cache

    def _evaluate_one(
        self,
        candidate: StagedCandidate,
        ohlcv_cache: dict[str, pd.DataFrame],
    ) -> Optional[dict]:
        """Evaluate a single CREATED candidate. Returns raw_candidate_dict if confirmed, None otherwise."""
        sym = candidate.symbol
        df = ohlcv_cache.get(sym)

        if df is None or len(df) < 10:
            logger.debug(
                "LTFScanWorker: skipping %s — no 15m data available (will retry next cycle)",
                candidate.candidate_id,
            )
            return None

        # Run the confirmation module
        result = evaluate_confirmation(df, candidate.side, cfg=self._cfg)

        logger.info(
            "LTFScanWorker: %s %s %s | score=%.3f | "
            "LTF: confirmed=%s voided=%s (checks=%d/3) | "
            "ema_slope=%.4f rsi=%.1f vol_ratio=%.2f",
            candidate.candidate_id, candidate.symbol, candidate.side,
            candidate.score,
            result.confirmed, result.voided, result.checks_passed,
            result.ema_slope, result.rsi, result.volume_ratio,
        )

        if result.voided:
            # Void the candidate — it strongly contradicts the LTF data
            self._store.void(
                candidate.candidate_id,
                reason=result.void_reason or "LTF contradiction",
            )
            return None

        if result.confirmed:
            # Confirm the candidate — all 3 LTF checks passed
            ok = self._store.confirm(
                candidate.candidate_id,
                ltf_confirmation_price=result.ltf_close,
                ltf_rsi=result.rsi,
                ltf_ema_aligned=result.ema_aligned,
                ltf_volume_ratio=result.volume_ratio,
            )
            if ok:
                # Return the raw candidate dict with LTF data appended
                enriched = dict(candidate.raw_candidate_dict)
                enriched["ltf_confirmed"] = True
                enriched["ltf_confirmation_price"] = result.ltf_close
                enriched["ltf_rsi"] = result.rsi
                enriched["ltf_ema_slope"] = result.ema_slope
                enriched["ltf_volume_ratio"] = result.volume_ratio
                enriched["staged_candidate_id"] = candidate.candidate_id
                return enriched
            else:
                logger.warning(
                    "LTFScanWorker: confirm() returned False for %s (state race?)",
                    candidate.candidate_id,
                )
                return None

        # Not confirmed, not voided → leave as CREATED for next cycle
        logger.debug(
            "LTFScanWorker: %s not yet confirmed (checks=%d/3) — will retry",
            candidate.candidate_id, result.checks_passed,
        )
        return None
