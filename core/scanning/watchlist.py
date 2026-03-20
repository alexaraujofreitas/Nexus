# ============================================================
# NEXUS TRADER — Watchlist Manager
#
# Manages named groups of symbols to scan.
# Stored in config.yaml under scanner.watchlists key.
# Supports: manual lists, tagging, enable/disable per list.
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


class WatchlistManager:
    """
    Manages named watchlists of trading symbols.
    Persisted to config.yaml.
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
        """Return deduplicated symbols from all enabled watchlists."""
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
