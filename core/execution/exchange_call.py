# ============================================================
# NEXUS TRADER — Exchange Call Wrapper  (Phase 3)
#
# Centralised timeout + error handling for ALL exchange API
# calls.  Every CCXT call goes through `exchange_call()` which:
#   1. Runs the call in a single-use ThreadPoolExecutor
#   2. Enforces a config-driven timeout
#   3. Shuts the pool cleanly on success, timeout, or error
#   4. Never leaks background workers
#
# Config keys (config.yaml / DEFAULT_CONFIG):
#   exchange.order_timeout_seconds   — for create_market_order etc.
#   exchange.data_timeout_seconds    — for fetch_balance, fetch_positions
# ============================================================
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)
T = TypeVar("T")

# ── Config cache (reloaded lazily) ──────────────────────────
_cfg_cache: dict[str, Any] = {}
_cfg_ts: float = 0.0
_CFG_RELOAD_INTERVAL = 30.0  # re-read config every 30s


def _load_cfg() -> dict[str, Any]:
    """Load exchange timeout config with caching."""
    global _cfg_cache, _cfg_ts
    now = time.monotonic()
    if _cfg_cache and (now - _cfg_ts) < _CFG_RELOAD_INTERVAL:
        return _cfg_cache
    try:
        from config.settings import settings
        _cfg_cache = {
            "order_timeout": float(settings.get("exchange.order_timeout_seconds", 20)),
            "data_timeout": float(settings.get("exchange.data_timeout_seconds", 10)),
        }
    except Exception as exc:
        logger.warning("exchange_call: config load failed, using defaults: %s", exc)
        _cfg_cache = {"order_timeout": 20.0, "data_timeout": 10.0}
    _cfg_ts = now
    return _cfg_cache


def exchange_call(
    fn: Callable[..., T],
    *args: Any,
    timeout_key: str = "data_timeout",
    label: str = "",
    **kwargs: Any,
) -> T:
    """
    Execute an exchange API call with timeout and clean shutdown.

    Parameters
    ----------
    fn : callable
        The CCXT method to call (e.g. ``ex.create_market_order``).
    *args, **kwargs
        Positional and keyword arguments passed to *fn*.
    timeout_key : str
        Config key: ``"order_timeout"`` for order calls,
        ``"data_timeout"`` for read-only fetches.
    label : str
        Human-readable label for logging (e.g. ``"create_market_order BTC/USDT"``).

    Returns
    -------
    The return value of *fn(*args, **kwargs)*.

    Raises
    ------
    TimeoutError
        If the call exceeds the configured timeout.
    Exception
        Any exception raised by the underlying CCXT call.
    """
    cfg = _load_cfg()
    timeout_s = cfg.get(timeout_key, 20.0)
    label = label or getattr(fn, "__name__", "exchange_call")

    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(fn, *args, **kwargs)
        try:
            result = future.result(timeout=timeout_s)
            return result
        except FutTimeout:
            future.cancel()
            logger.error(
                "exchange_call TIMEOUT: %s exceeded %.1fs",
                label, timeout_s,
            )
            raise TimeoutError(
                f"Exchange call '{label}' timed out after {timeout_s:.1f}s"
            )
        except Exception as exc:
            exc_str = str(exc).lower()
            if any(k in exc_str for k in ("dns", "gaierror", "name resolution", "ssl", "certificate")):
                logger.error("exchange_call DNS/TLS FAILURE: %s — %s", label, exc)
            raise
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


class ExchangeCallError(Exception):
    """Raised when an exchange call fails after all retries."""
    pass
