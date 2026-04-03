# ============================================================
# NEXUS TRADER Web — Structured Logging Configuration
#
# Phase 6A: Provides JSON and plain-text formatters.
# Format controlled by NEXUS_LOG_FORMAT env var:
#   "json"  → structured JSON (production)
#   "text"  → plain text (development, default)
#
# Log level controlled by NEXUS_LOG_LEVEL env var (default: INFO).
#
# Sensitive data (JWT tokens, passwords, API keys) is masked
# automatically in JSON output.
# ============================================================
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

# Patterns for sensitive data masking
_KV_PATTERNS = [
    # Key-value patterns: match "key=value" or "key: value" etc.
    # Character class excludes < > to avoid re-masking already-masked values
    re.compile(r'(password["\s:=]+)[^\s,}<>"\']{3,}', re.IGNORECASE),
    re.compile(r'(api[_-]?key["\s:=]+)[^\s,}<>"\']{3,}', re.IGNORECASE),
    re.compile(r'(secret["\s:=]+)[^\s,}<>"\']{3,}', re.IGNORECASE),
    re.compile(r'(token["\s:=]+)[^\s,}<>"\']{3,}', re.IGNORECASE),
]
_JWT_PATTERN = re.compile(r'eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}')


def _mask_sensitive(text: str) -> str:
    """Mask JWT tokens, passwords, API keys in log output.

    JWT tokens are matched first and replaced with <JWT_MASKED>.
    Then key-value patterns (password=, token=, secret=, api_key=) are masked.
    This order ensures JWT tokens inside token=<value> are properly handled.
    """
    # Step 1: Replace JWT tokens first
    text = _JWT_PATTERN.sub("<JWT_MASKED>", text)
    # Step 2: Replace key-value sensitive patterns (won't touch <JWT_MASKED>)
    for pattern in _KV_PATTERNS:
        text = pattern.sub(r"\1<MASKED>", text)
    return text


class JSONFormatter(logging.Formatter):
    """
    Structured JSON log formatter.
    Output: {"timestamp", "level", "logger", "message", "request_id", "extra"}
    """

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": _mask_sensitive(record.getMessage()),
        }

        # Include request_id if present (from AuditMiddleware extras)
        request_id = getattr(record, "request_id", None)
        if request_id:
            entry["request_id"] = request_id

        # Include any extra fields from logger.info(..., extra={...})
        _STANDARD_ATTRS = {
            "name", "msg", "args", "created", "relativeCreated", "exc_info",
            "exc_text", "stack_info", "lineno", "funcName", "filename",
            "module", "pathname", "thread", "threadName", "process",
            "processName", "levelname", "levelno", "message", "msecs",
            "taskName",
        }
        extras = {}
        for key, val in record.__dict__.items():
            if key.startswith("_") or key in _STANDARD_ATTRS:
                continue
            if key == "request_id":
                continue  # already handled
            extras[key] = val
        if extras:
            entry["extra"] = extras

        # Exception info
        if record.exc_info and record.exc_info[1]:
            entry["exception"] = _mask_sensitive(self.formatException(record.exc_info))

        return json.dumps(entry, default=str)


class PlainFormatter(logging.Formatter):
    """Standard plain-text formatter for development."""

    def __init__(self):
        super().__init__(
            fmt="%(asctime)s | %(name)-30s | %(levelname)-7s | %(message)s",
            datefmt=None,
        )


def configure_logging() -> None:
    """
    Configure root logger based on environment variables:
      NEXUS_LOG_FORMAT: "json" | "text" (default: "text")
      NEXUS_LOG_LEVEL:  DEBUG | INFO | WARNING | ERROR (default: "INFO")
    """
    log_format = os.getenv("NEXUS_LOG_FORMAT", "text").lower()
    log_level = os.getenv("NEXUS_LOG_LEVEL", "INFO").upper()

    # Validate level
    numeric_level = getattr(logging, log_level, None)
    if not isinstance(numeric_level, int):
        numeric_level = logging.INFO

    # Choose formatter
    if log_format == "json":
        formatter: logging.Formatter = JSONFormatter()
    else:
        formatter = PlainFormatter()

    # Configure root logger
    root = logging.getLogger()
    root.setLevel(numeric_level)

    # Remove existing handlers to avoid duplicates on re-configure
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    # Quiet noisy libraries
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
