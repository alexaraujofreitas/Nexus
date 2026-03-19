# ============================================================
# NEXUS TRADER — Regime Model Retrainer  (Sprint 15)
#
# Schedules monthly retraining of the HMM regime classifier
# using the most recent 90 days of OHLCV data from the DB.
#
# Runs as a background daemon thread. Retraining is triggered:
#   - Once at startup (if last retrain > 30 days ago)
#   - Every 30 days thereafter
#
# The HMM model is saved to data/models/hmm_regime_classifier.pkl
# and automatically loaded by HMMRegimeClassifier on next use.
# ============================================================
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

RETRAIN_INTERVAL_DAYS = 30
TRAINING_HISTORY_DAYS = 90
MODEL_PATH = Path(__file__).parent.parent.parent / "data" / "models" / "hmm_regime_classifier.pkl"
LAST_RETRAIN_PATH = Path(__file__).parent.parent.parent / "data" / "models" / ".hmm_last_retrain"


class RegimeRetrainer:
    """
    Background daemon that retrains the HMM regime classifier monthly.
    """

    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._last_retrain: Optional[datetime] = None
        self._retrain_count: int = 0
        self._load_last_retrain_time()

    def start(self) -> None:
        """Start the background retraining scheduler."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._scheduler_loop,
            name="HMMRetrainer",
            daemon=True,
        )
        self._thread.start()
        logger.info("RegimeRetrainer: scheduler started (interval=%dd)", RETRAIN_INTERVAL_DAYS)

    def stop(self) -> None:
        """Stop the retraining scheduler."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("RegimeRetrainer: scheduler stopped")

    def trigger_retrain(self) -> bool:
        """
        Manually trigger an immediate retraining.
        Returns True if retrain was successful.
        """
        return self._run_retrain()

    def get_status(self) -> dict:
        """Return retraining status for UI display."""
        with self._lock:
            return {
                "last_retrain": self._last_retrain.isoformat() if self._last_retrain else None,
                "retrain_count": self._retrain_count,
                "next_retrain": self._get_next_retrain_time(),
                "model_path": str(MODEL_PATH),
                "model_exists": MODEL_PATH.exists(),
            }

    # ── Private ───────────────────────────────────────────────

    def _scheduler_loop(self) -> None:
        """Main scheduler loop — checks every hour if retrain is due."""
        # Check immediately on startup
        if self._should_retrain():
            logger.info("RegimeRetrainer: initial retrain triggered (overdue)")
            self._run_retrain()

        while not self._stop_event.is_set():
            # Sleep 1 hour between checks
            self._stop_event.wait(timeout=3600)
            if self._stop_event.is_set():
                break
            if self._should_retrain():
                self._run_retrain()

    def _should_retrain(self) -> bool:
        """Return True if retraining is overdue."""
        with self._lock:
            if self._last_retrain is None:
                return True
            age = (datetime.now(timezone.utc) - self._last_retrain).days
            return age >= RETRAIN_INTERVAL_DAYS

    def _run_retrain(self) -> bool:
        """Execute HMM retraining. Returns True on success."""
        logger.info("RegimeRetrainer: starting HMM retraining...")
        try:
            data = self._load_training_data()
            if data is None or len(data) < 100:
                logger.warning("RegimeRetrainer: insufficient training data — skipping")
                return False

            success = self._retrain_hmm(data)
            if success:
                with self._lock:
                    self._last_retrain = datetime.now(timezone.utc)
                    self._retrain_count += 1
                self._save_last_retrain_time()

                try:
                    from core.event_bus import bus, Topics
                    bus.publish(Topics.SYSTEM_ALERT, {
                        "title": "HMM Regime Model Retrained",
                        "message": f"Regime classifier retrained on {TRAINING_HISTORY_DAYS} days of data. "
                                   f"Retraining #{self._retrain_count}.",
                    }, source="regime_retrainer")
                except Exception:
                    pass

                logger.info("RegimeRetrainer: retraining complete (#%d)", self._retrain_count)
            return success

        except Exception as exc:
            logger.error("RegimeRetrainer: retraining failed — %s", exc, exc_info=True)
            return False

    def _load_training_data(self):
        """Load recent OHLCV data from the database for training."""
        try:
            import pandas as pd
            from datetime import timezone as _tz
            from core.database.engine import get_session

            # Try to get OHLCV from DB cache or market data
            cutoff = datetime.now(_tz.utc) - timedelta(days=TRAINING_HISTORY_DAYS)

            # Attempt to fetch from the active exchange (whatever the user has configured)
            try:
                from core.market_data.exchange_manager import exchange_manager
                ex = exchange_manager.get_exchange()
                if ex:
                    # Resolve a reliable BTC pair — try both spot and linear-perp notation
                    btc_symbol = "BTC/USDT"
                    if btc_symbol not in (ex.markets or {}):
                        for candidate in ("BTCUSDT", "BTC/USDT:USDT", "BTC-USDT"):
                            if candidate in (ex.markets or {}):
                                btc_symbol = candidate
                                break
                    ohlcv = ex.fetch_ohlcv(
                        btc_symbol, timeframe="4h",
                        limit=TRAINING_HISTORY_DAYS * 6,
                    )
                    if ohlcv and len(ohlcv) >= 100:
                        df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
                        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
                        return df
            except Exception:
                pass

            # Fallback: try to load cached data
            cache_dir = Path(__file__).parent.parent.parent / "data" / "cache"
            cache_file = cache_dir / "BTC_USDT_4h_training.csv"
            if cache_file.exists():
                df = pd.read_csv(cache_file)
                if len(df) >= 100:
                    return df

            return None
        except Exception as exc:
            logger.debug("RegimeRetrainer: data load failed — %s", exc)
            return None

    def _retrain_hmm(self, data) -> bool:
        """Retrain the HMM model on the provided data."""
        try:
            # Check if hmmlearn is available
            try:
                from hmmlearn import hmm
            except ImportError:
                logger.warning("RegimeRetrainer: hmmlearn not installed — using simplified retraining")
                return self._retrain_simplified(data)

            import numpy as np
            import pickle

            # Feature extraction
            returns = data["close"].pct_change().dropna().values
            log_returns = np.log1p(returns).reshape(-1, 1)

            # Train Gaussian HMM with 6 hidden states (matching original)
            model = hmm.GaussianHMM(
                n_components=6,
                covariance_type="full",
                n_iter=100,
                random_state=42,
            )
            model.fit(log_returns)

            # Save model
            MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(MODEL_PATH, "wb") as f:
                pickle.dump(model, f)

            logger.info("RegimeRetrainer: HMM saved to %s", MODEL_PATH)
            return True

        except Exception as exc:
            logger.error("RegimeRetrainer: HMM training error — %s", exc)
            return False

    def _retrain_simplified(self, data) -> bool:
        """Simplified parameter update when hmmlearn is unavailable."""
        try:
            import json
            # Just update thresholds based on recent volatility
            returns = data["close"].pct_change().dropna()
            vol = float(returns.std() * 100)

            params = {
                "recent_volatility": vol,
                "retrained_at": datetime.now(timezone.utc).isoformat(),
                "samples": len(data),
            }
            params_path = MODEL_PATH.parent / "hmm_params.json"
            params_path.parent.mkdir(parents=True, exist_ok=True)
            with open(params_path, "w") as f:
                json.dump(params, f, indent=2)
            return True
        except Exception as exc:
            logger.debug("RegimeRetrainer: simplified retrain failed — %s", exc)
            return False

    def _load_last_retrain_time(self) -> None:
        """Load last retrain timestamp from disk."""
        try:
            if LAST_RETRAIN_PATH.exists():
                ts_str = LAST_RETRAIN_PATH.read_text().strip()
                self._last_retrain = datetime.fromisoformat(ts_str)
        except Exception:
            self._last_retrain = None

    def _save_last_retrain_time(self) -> None:
        """Save last retrain timestamp to disk."""
        try:
            LAST_RETRAIN_PATH.parent.mkdir(parents=True, exist_ok=True)
            LAST_RETRAIN_PATH.write_text(datetime.now(timezone.utc).isoformat())
        except Exception as exc:
            logger.debug("RegimeRetrainer: could not save retrain time — %s", exc)

    def _get_next_retrain_time(self) -> Optional[str]:
        """Return ISO timestamp of next scheduled retrain."""
        if self._last_retrain is None:
            return "overdue"
        next_dt = self._last_retrain + timedelta(days=RETRAIN_INTERVAL_DAYS)
        return next_dt.isoformat()


# ── Module-level singleton ────────────────────────────────────
_retrainer: Optional[RegimeRetrainer] = None


def get_regime_retrainer() -> RegimeRetrainer:
    global _retrainer
    if _retrainer is None:
        _retrainer = RegimeRetrainer()
    return _retrainer
