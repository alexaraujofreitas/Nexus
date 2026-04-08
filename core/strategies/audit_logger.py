# ============================================================
# NEXUS TRADER — Strategy Audit Logger
# ============================================================
# Append-only JSONL logger for strategy parameter changes.
# Thread-safe with automatic file rotation and cleanup.

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, List, Dict

from config.constants import DATA_DIR


class AuditLogger:
    """
    Thread-safe append-only audit logger for strategy parameter changes.

    Maintains a JSON Lines file (one JSON object per line) with full history
    of parameter changes, restores, and model state transitions.

    Attributes:
        log_file (Path): Path to JSONL audit log (data/strategy_audit.jsonl)
        _lock (threading.Lock): Synchronizes concurrent writes
    """

    # Action types
    ACTION_PARAM_CHANGE = "param_change"
    ACTION_RESTORE_DEFAULTS = "restore_defaults"
    ACTION_RESTORE_VERSION = "restore_version"
    ACTION_MODEL_ENABLED = "model_enabled"
    ACTION_MODEL_DISABLED = "model_disabled"

    VALID_ACTIONS = {
        ACTION_PARAM_CHANGE,
        ACTION_RESTORE_DEFAULTS,
        ACTION_RESTORE_VERSION,
        ACTION_MODEL_ENABLED,
        ACTION_MODEL_DISABLED,
    }

    def __init__(self, log_file: Optional[Path] = None):
        """
        Initialize the audit logger.

        Args:
            log_file: Path to JSONL log file. Defaults to data/strategy_audit.jsonl
        """
        self.log_file = log_file or (DATA_DIR / "strategy_audit.jsonl")
        self._lock = threading.Lock()

        # Ensure parent directory exists
        self.log_file.parent.mkdir(parents=True, exist_ok=True)

        # Create file if it doesn't exist
        if not self.log_file.exists():
            self.log_file.touch()

    def log_change(
        self,
        action: str,
        model: str,
        key: str,
        old_value: Any,
        new_value: Any,
        config_version: Optional[int] = None,
    ) -> None:
        """
        Log a parameter change or state transition.

        Args:
            action: One of ACTION_* constants (param_change, restore_defaults, etc.)
            model: Model name (e.g., "trend_model", "rl_ensemble")
            key: Parameter name (e.g., "enabled", "threshold", "weight")
            old_value: Previous value
            new_value: New value
            config_version: Optional version number if part of a restore

        Raises:
            ValueError: If action is not a recognized type
        """
        if action not in self.VALID_ACTIONS:
            raise ValueError(
                f"Invalid action '{action}'. Must be one of: {self.VALID_ACTIONS}"
            )

        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "model": model,
            "key": key,
            "old": old_value,
            "new": new_value,
            "version": config_version,
        }

        # Thread-safe append to JSONL file
        with self._lock:
            try:
                with open(self.log_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, default=str) + "\n")
            except Exception as exc:
                # Log failures are non-fatal but should be visible
                logging.getLogger(__name__).warning("AuditLogger: log_change write failed: %s", exc)

    def get_log(
        self,
        model: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        Retrieve audit log entries, optionally filtered by model.

        Args:
            model: Filter by model name (e.g., "trend_model"). If None, return all.
            limit: Maximum number of entries to return (most recent first)

        Returns:
            List of audit entry dicts, sorted newest-first
        """
        entries = []

        with self._lock:
            if not self.log_file.exists():
                return entries

            try:
                with open(self.log_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue

                        try:
                            entry = json.loads(line)
                            if model is None or entry.get("model") == model:
                                entries.append(entry)
                        except json.JSONDecodeError:
                            # Skip malformed lines
                            pass
            except Exception:
                # File read errors are non-fatal
                return entries

        # Return newest first, up to limit
        return sorted(
            entries,
            key=lambda e: e.get("ts", ""),
            reverse=True,
        )[:limit]

    def get_last_change(self, model: str, key: str) -> Optional[Dict[str, Any]]:
        """
        Get the most recent change for a specific model + key combination.

        Args:
            model: Model name
            key: Parameter name

        Returns:
            The entry dict, or None if no change found
        """
        with self._lock:
            if not self.log_file.exists():
                return None

            try:
                with open(self.log_file, "r", encoding="utf-8") as f:
                    for line in reversed(f.readlines()):
                        line = line.strip()
                        if not line:
                            continue

                        try:
                            entry = json.loads(line)
                            if entry.get("model") == model and entry.get("key") == key:
                                return entry
                        except json.JSONDecodeError:
                            pass
            except Exception:
                pass

        return None

    def count_entries(self) -> int:
        """Return total number of audit log entries."""
        count = 0

        with self._lock:
            if not self.log_file.exists():
                return 0

            try:
                with open(self.log_file, "r", encoding="utf-8") as f:
                    count = sum(1 for line in f if line.strip())
            except Exception:
                pass

        return count

    def clear(self) -> None:
        """Clear the entire audit log. Use with caution."""
        with self._lock:
            try:
                self.log_file.write_text("")
            except Exception as exc:
                logging.getLogger(__name__).warning("AuditLogger: clear failed: %s", exc)


# Module singleton
_logger_instance: Optional[AuditLogger] = None


def get_audit_logger() -> AuditLogger:
    """
    Get or create the singleton AuditLogger instance.

    Returns:
        The module-level AuditLogger instance
    """
    global _logger_instance
    if _logger_instance is None:
        _logger_instance = AuditLogger()
    return _logger_instance
