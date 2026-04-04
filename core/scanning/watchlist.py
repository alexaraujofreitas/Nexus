# ============================================================
# NEXUS TRADER — Watchlist Manager
#
# Manages named groups of symbols to scan.
#
# Phase 3A (web mode): When PostgreSQL is available, the scan
# universe is read from Asset.is_tradable on the active exchange.
# This makes the web Asset Management page the SINGLE SOURCE
# OF TRUTH for which symbols the scanner scans.
#
# Desktop mode: Falls back to config.yaml under scanner.watchlists.
# ============================================================
from __future__ import annotations

import logging
from typing import Optional
from config.settings import settings

logger = logging.getLogger(__name__)

DEFAULT_WATCHLIST = {
    "Default": {
        "symbols": ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT"],
        "enabled": True,
        "description": "Major crypto assets",
    }
}


def _try_db_tradable_symbols() -> Optional[list[str]]:
    """
    Attempt to read tradable symbols from PostgreSQL (web mode).

    Returns a list of symbols if PostgreSQL is available and the
    active exchange has tradable assets; returns None if PostgreSQL
    is unreachable or not configured (desktop-only mode).
    """
    try:
        from app.database import get_sync_session
        from app.models.trading import Asset, Exchange
        from sqlalchemy import select
    except ImportError:
        # app.database not on sys.path → desktop-only mode
        return None

    try:
        with get_sync_session() as session:
            # Find the active exchange
            active_ex = session.execute(
                select(Exchange).where(Exchange.is_active.is_(True))
            ).scalar_one_or_none()
            if active_ex is None:
                logger.debug("WatchlistManager: no active exchange in DB")
                return None

            rows = session.execute(
                select(Asset.symbol)
                .where(
                    Asset.exchange_id == active_ex.id,
                    Asset.is_tradable.is_(True),
                )
                .order_by(Asset.symbol)
            ).scalars().all()

            symbols = [s.upper().strip() for s in rows]
            logger.info(
                "WatchlistManager: DB source → %d tradable symbols on %s",
                len(symbols), active_ex.name,
            )
            return symbols
    except Exception as exc:
        logger.warning(
            "WatchlistManager: PostgreSQL query failed, falling back to config: %s", exc,
        )
        return None


class WatchlistManager:
    """
    Manages named watchlists of trading symbols.

    Web mode (Phase 3A): delegates to PostgreSQL Asset.is_tradable.
    Desktop mode: persisted to config.yaml.
    """

    _SETTING_KEY = "scanner.watchlists"

    def get_all(self) -> dict:
        """Return all watchlists dict."""
        wl = settings.get(self._SETTING_KEY, None)
        if not wl:
            self._save(DEFAULT_WATCHLIST)
            return DEFAULT_WATCHLIST
        return wl

    def get_active_symbols(self) -> list[str]:
        """
        Return deduplicated symbols for the scan universe.

        Primary (web mode): PostgreSQL Asset.is_tradable == True
        on the active exchange.  Falls through to config.yaml if
        PostgreSQL is unavailable, not configured, or returns empty.
        """
        # ── Phase 3A: DB-authoritative path ──────────────────
        db_symbols = _try_db_tradable_symbols()
        if db_symbols is not None:
            # DB answered (even if empty list — that means user
            # deliberately has zero tradable assets).  DB is SOT.
            return db_symbols

        # ── Fallback: config.yaml (desktop-only) ─────────────
        seen: set[str] = set()
        out:  list[str] = []
        for wl_data in self.get_all().values():
            if not wl_data.get("enabled", True):
                continue
            for sym in wl_data.get("symbols", []):
                sym_upper = sym.upper().strip()
                if sym_upper not in seen:
                    seen.add(sym_upper)
                    out.append(sym_upper)
        return out

    def get_watchlist(self, name: str) -> Optional[dict]:
        return self.get_all().get(name)

    def create_watchlist(self, name: str, symbols: list[str], description: str = "") -> None:
        all_wl = self.get_all()
        all_wl[name] = {
            "symbols":     [s.upper().strip() for s in symbols],
            "enabled":     True,
            "description": description,
        }
        self._save(all_wl)
        logger.info("Watchlist %r created (%d symbols)", name, len(symbols))

    def update_watchlist(self, name: str, symbols: list[str]) -> bool:
        all_wl = self.get_all()
        if name not in all_wl:
            return False
        all_wl[name]["symbols"] = [s.upper().strip() for s in symbols]
        self._save(all_wl)
        return True

    def add_symbol(self, watchlist_name: str, symbol: str) -> bool:
        all_wl = self.get_all()
        if watchlist_name not in all_wl:
            return False
        sym = symbol.upper().strip()
        if sym not in all_wl[watchlist_name]["symbols"]:
            all_wl[watchlist_name]["symbols"].append(sym)
            self._save(all_wl)
        return True

    def remove_symbol(self, watchlist_name: str, symbol: str) -> bool:
        all_wl = self.get_all()
        if watchlist_name not in all_wl:
            return False
        sym = symbol.upper().strip()
        all_wl[watchlist_name]["symbols"] = [
            s for s in all_wl[watchlist_name]["symbols"] if s != sym
        ]
        self._save(all_wl)
        return True

    def set_enabled(self, name: str, enabled: bool) -> bool:
        all_wl = self.get_all()
        if name not in all_wl:
            return False
        all_wl[name]["enabled"] = enabled
        self._save(all_wl)
        return True

    def delete_watchlist(self, name: str) -> bool:
        all_wl = self.get_all()
        if name not in all_wl:
            return False
        del all_wl[name]
        self._save(all_wl)
        return True

    def _save(self, data: dict) -> None:
        settings.set(self._SETTING_KEY, data)
