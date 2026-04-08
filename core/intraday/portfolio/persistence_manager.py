# ============================================================
# NEXUS TRADER — Persistence Manager (Phase 5)
#
# JSON/JSONL persistence for positions and trade ledger.
# Retry logic with corruption handling.
# ============================================================
import json
import logging
import time
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)


class PersistenceManager:
    """
    Handles file I/O for intraday positions (JSON) and trades (JSONL).
    Retry logic (3x with 100ms backoff). Corruption detection and recovery.
    """

    def __init__(
        self,
        positions_path: str = "data/intraday_positions.json",
        trades_path: str = "data/intraday_trades.jsonl",
        retry_attempts: int = 3,
        retry_backoff_ms: int = 100,
    ) -> None:
        """
        Initialize persistence paths.

        Args:
            positions_path: Path to positions JSON file
            trades_path: Path to trades JSONL file
            retry_attempts: Number of write retries
            retry_backoff_ms: Backoff time between retries (ms)
        """
        self.positions_path = Path(positions_path)
        self.trades_path = Path(trades_path)
        self.retry_attempts = retry_attempts
        self.retry_backoff_ms = retry_backoff_ms

        # Ensure directories exist
        self.positions_path.parent.mkdir(parents=True, exist_ok=True)
        self.trades_path.parent.mkdir(parents=True, exist_ok=True)

        logger.info(
            f"PersistenceManager initialized: "
            f"positions={self.positions_path}, trades={self.trades_path}"
        )

    def save_positions(self, positions: List[dict]) -> bool:
        """
        Save positions list to JSON.

        Args:
            positions: List of position dicts

        Returns:
            True if successful, False otherwise
        """
        return self._write_json(self.positions_path, positions, "positions")

    def save_trade(self, trade: dict) -> bool:
        """
        Append a single trade to JSONL file.

        Args:
            trade: Trade dict to append

        Returns:
            True if successful, False otherwise
        """
        for attempt in range(self.retry_attempts):
            try:
                with open(self.trades_path, "a") as f:
                    f.write(json.dumps(trade) + "\n")
                logger.debug(f"save_trade: appended {trade.get('position_id', '?')}")
                return True
            except Exception as e:
                if attempt < self.retry_attempts - 1:
                    wait_ms = self.retry_backoff_ms * (attempt + 1)
                    logger.warning(
                        f"save_trade: attempt {attempt + 1} failed, retrying in {wait_ms}ms: {e}"
                    )
                    time.sleep(wait_ms / 1000.0)
                else:
                    logger.error(f"save_trade: all retries exhausted: {e}")
                    return False

        return False

    def load_positions(self) -> List[dict]:
        """
        Load positions from JSON.

        Returns:
            List of position dicts, or empty list if file missing/corrupted
        """
        if not self.positions_path.exists():
            logger.debug(f"load_positions: file not found at {self.positions_path}")
            return []

        try:
            with open(self.positions_path, "r") as f:
                content = f.read()
                if not content.strip():
                    logger.debug(f"load_positions: file is empty")
                    return []
                data = json.loads(content)
                if not isinstance(data, list):
                    logger.error(
                        f"load_positions: expected list, got {type(data).__name__}"
                    )
                    return []
                logger.info(f"load_positions: loaded {len(data)} positions")
                return data
        except json.JSONDecodeError as e:
            logger.critical(
                f"load_positions: JSON corruption at {self.positions_path}: {e}. "
                f"Returning empty list."
            )
            return []
        except Exception as e:
            logger.error(f"load_positions: I/O error: {e}")
            return []

    def load_trades(self) -> List[dict]:
        """
        Load trades from JSONL.

        Returns:
            List of trade dicts, or empty list if file missing/corrupted
        """
        if not self.trades_path.exists():
            logger.debug(f"load_trades: file not found at {self.trades_path}")
            return []

        trades = []
        try:
            with open(self.trades_path, "r") as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        trade = json.loads(line)
                        trades.append(trade)
                    except json.JSONDecodeError as e:
                        logger.critical(
                            f"load_trades: JSON corruption on line {line_num} "
                            f"at {self.trades_path}: {e}. Skipping line."
                        )
                        # Continue to load rest of file
                        continue
            logger.info(f"load_trades: loaded {len(trades)} trades")
            return trades
        except Exception as e:
            logger.error(f"load_trades: I/O error: {e}")
            return trades

    def _write_json(self, path: Path, data: any, label: str) -> bool:
        """
        Write data to JSON file with retry logic.

        Args:
            path: File path
            data: Data to write
            label: Label for logging

        Returns:
            True if successful, False otherwise
        """
        for attempt in range(self.retry_attempts):
            try:
                with open(path, "w") as f:
                    json.dump(data, f, indent=2)
                logger.debug(f"_write_json: {label} written to {path}")
                return True
            except Exception as e:
                if attempt < self.retry_attempts - 1:
                    wait_ms = self.retry_backoff_ms * (attempt + 1)
                    logger.warning(
                        f"_write_json: {label} attempt {attempt + 1} failed, "
                        f"retrying in {wait_ms}ms: {e}"
                    )
                    time.sleep(wait_ms / 1000.0)
                else:
                    logger.error(f"_write_json: {label} all retries exhausted: {e}")
                    return False

        return False
