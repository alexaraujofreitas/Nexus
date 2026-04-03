# ============================================================
# Phase 6A Tests — Structured Logging & Audit Middleware
#
# Validates:
#   - JSON formatter produces valid JSON with required fields
#   - Sensitive data masking (JWT, password, secret, token)
#   - PlainFormatter produces expected format
#   - configure_logging() switches between JSON/text
#   - AuditMiddleware excludes /health paths
#   - AuditMiddleware generates request_id
# ============================================================
from __future__ import annotations

import json
import logging
import os

import pytest

pytestmark = pytest.mark.unit


class TestJSONFormatter:
    """Test the JSONFormatter class."""

    def _make_record(self, msg: str, **extra):
        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg=msg,
            args=(),
            exc_info=None,
        )
        for k, v in extra.items():
            setattr(record, k, v)
        return record

    def test_produces_valid_json(self):
        from app.logging_config import JSONFormatter
        fmt = JSONFormatter()
        record = self._make_record("test message")
        output = fmt.format(record)
        parsed = json.loads(output)
        assert parsed["level"] == "INFO"
        assert parsed["logger"] == "test.logger"
        assert parsed["message"] == "test message"
        assert "timestamp" in parsed

    def test_includes_request_id(self):
        from app.logging_config import JSONFormatter
        fmt = JSONFormatter()
        record = self._make_record("msg", request_id="abc-123")
        parsed = json.loads(fmt.format(record))
        assert parsed["request_id"] == "abc-123"

    def test_includes_extra_fields(self):
        from app.logging_config import JSONFormatter
        fmt = JSONFormatter()
        record = self._make_record("msg", method="GET", path="/api/v1/foo")
        parsed = json.loads(fmt.format(record))
        assert parsed["extra"]["method"] == "GET"
        assert parsed["extra"]["path"] == "/api/v1/foo"


class TestSensitiveMasking:
    """Test that sensitive data is masked in log output."""

    def test_jwt_token_masked(self):
        from app.logging_config import _mask_sensitive
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIiwiZW1haWwiOiJhQGIuY29tIn0.abc123signature"
        result = _mask_sensitive(f"token={jwt}")
        assert "eyJ" not in result
        assert "<JWT_MASKED>" in result

    def test_password_masked(self):
        from app.logging_config import _mask_sensitive
        result = _mask_sensitive('password: mysecretpassword')
        assert "mysecretpassword" not in result
        assert "<MASKED>" in result

    def test_api_key_masked(self):
        from app.logging_config import _mask_sensitive
        result = _mask_sensitive('api_key="sk-1234567890"')
        assert "sk-1234567890" not in result
        assert "<MASKED>" in result

    def test_plain_message_unchanged(self):
        from app.logging_config import _mask_sensitive
        msg = "Server started on port 8000"
        assert _mask_sensitive(msg) == msg


class TestConfigureLogging:
    """Test configure_logging() switches between formats."""

    def test_text_format_default(self):
        os.environ.pop("NEXUS_LOG_FORMAT", None)
        from app.logging_config import configure_logging, PlainFormatter
        configure_logging()
        root = logging.getLogger()
        assert any(isinstance(h.formatter, PlainFormatter) for h in root.handlers)

    def test_json_format(self):
        os.environ["NEXUS_LOG_FORMAT"] = "json"
        try:
            from app.logging_config import configure_logging, JSONFormatter
            configure_logging()
            root = logging.getLogger()
            assert any(isinstance(h.formatter, JSONFormatter) for h in root.handlers)
        finally:
            os.environ.pop("NEXUS_LOG_FORMAT", None)

    def test_log_level_from_env(self):
        os.environ["NEXUS_LOG_LEVEL"] = "DEBUG"
        try:
            from app.logging_config import configure_logging
            configure_logging()
            root = logging.getLogger()
            assert root.level == logging.DEBUG
        finally:
            os.environ.pop("NEXUS_LOG_LEVEL", None)


class TestAuditMiddleware:
    """Test the AuditMiddleware request logging and X-Request-ID."""

    @pytest.fixture
    def client(self):
        os.environ.setdefault("NEXUS_DATABASE_URL", "sqlite+aiosqlite:///")
        os.environ.setdefault("NEXUS_REDIS_URL", "redis://localhost:6379/0")
        os.environ.setdefault("NEXUS_JWT_SECRET", "test-secret-key-32chars-long!!")

        from app.config import clear_settings
        clear_settings()

        from main import app
        from fastapi.testclient import TestClient
        return TestClient(app)

    def test_request_id_generated(self, client):
        """Every response has X-Request-ID header."""
        resp = client.get("/")
        assert "X-Request-ID" in resp.headers
        # UUID4 format: 8-4-4-4-12
        rid = resp.headers["X-Request-ID"]
        assert len(rid) == 36

    def test_forwarded_request_id_preserved(self, client):
        """If client sends X-Request-ID, it is preserved."""
        resp = client.get("/", headers={"X-Request-ID": "custom-rid-123"})
        assert resp.headers["X-Request-ID"] == "custom-rid-123"

    def test_health_excluded_from_audit(self, client, caplog):
        """Health endpoints do not produce audit log lines."""
        with caplog.at_level(logging.INFO, logger="nexus.audit"):
            client.get("/health")
        audit_lines = [r for r in caplog.records if r.name == "nexus.audit"]
        assert len(audit_lines) == 0

    def test_normal_request_produces_audit_log(self, client, caplog):
        """Non-health requests produce an audit log entry."""
        with caplog.at_level(logging.INFO, logger="nexus.audit"):
            client.get("/")
        audit_lines = [r for r in caplog.records if r.name == "nexus.audit"]
        assert len(audit_lines) >= 1
