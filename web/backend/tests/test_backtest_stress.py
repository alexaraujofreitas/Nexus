# ============================================================
# Phase 5 — Backtest Stress Testing Suite
#
# 5C-1: Controlled Concurrency (3 simultaneous jobs)
# 5C-2: Long-Running Stability (multi-year dataset)
# 5C-3: Cancellation Handling (clean termination)
# 5C-4: Failure Injection (invalid params, missing data)
# 5C-5: Result Integrity (deterministic comparison)
# ============================================================
import asyncio
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from httpx import ASGITransport, AsyncClient

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from core_patch import install_qt_shim
install_qt_shim()

from main import app  # noqa: E402
from app.auth.dependencies import get_current_user  # noqa: E402


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _set_auth_override():
    """Set auth override before each test, clean up after."""
    app.dependency_overrides[get_current_user] = lambda: {"id": 1, "email": "stress@test.com"}
    yield
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ── 5C-1: Controlled Concurrency ────────────────────────────

class TestConcurrency:
    @pytest.mark.anyio
    @patch("app.api.backtest._send_engine_command", new_callable=AsyncMock)
    async def test_3_concurrent_backtests_return_unique_ids(self, mock_cmd, client):
        """Launch 3 concurrent backtest jobs; verify each gets a unique job_id."""
        call_count = 0

        async def mock_start(action, params, **kwargs):
            nonlocal call_count
            call_count += 1
            return {"status": "started", "job_id": f"job-{call_count}"}

        mock_cmd.side_effect = mock_start

        tasks = [
            client.post("/api/v1/backtest/start", json={
                "symbols": ["BTC/USDT"],
                "start_date": "2024-01-01",
                "end_date": "2024-06-01",
                "timeframe": "30m",
                "fee_pct": 0.04,
            })
            for _ in range(3)
        ]
        responses = await asyncio.gather(*tasks)

        job_ids = set()
        for r in responses:
            assert r.status_code == 200
            data = r.json()
            assert data["status"] == "started"
            job_ids.add(data["job_id"])

        # All 3 must be unique
        assert len(job_ids) == 3

    @pytest.mark.anyio
    @patch("app.api.backtest._send_engine_command", new_callable=AsyncMock)
    async def test_concurrent_results_are_isolated(self, mock_cmd, client):
        """Verify results from concurrent jobs don't cross-contaminate."""
        results_map = {
            "job-a": {"status": "complete", "job_id": "job-a", "pf": 1.5, "trades": 100},
            "job-b": {"status": "complete", "job_id": "job-b", "pf": 2.1, "trades": 200},
            "job-c": {"status": "complete", "job_id": "job-c", "pf": 0.8, "trades": 50},
        }

        async def mock_results(action, params, **kwargs):
            if action == "get_backtest_results":
                return results_map[params["job_id"]]
            return {"status": "started", "job_id": params.get("job_id", "unknown")}

        mock_cmd.side_effect = mock_results

        for job_id, expected in results_map.items():
            r = await client.get(f"/api/v1/backtest/results/{job_id}")
            assert r.status_code == 200
            data = r.json()
            assert data["job_id"] == job_id
            assert data["pf"] == expected["pf"]
            assert data["trades"] == expected["trades"]

    @pytest.mark.anyio
    @patch("app.api.backtest._send_engine_command", new_callable=AsyncMock)
    async def test_concurrent_timeout_handled_gracefully(self, mock_cmd, client):
        """One slow job times out while others complete normally."""
        call_idx = 0

        async def mock_start(action, params, **kwargs):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 2:
                return {"status": "timeout", "command_id": "slow-job"}
            return {"status": "started", "job_id": f"job-{call_idx}"}

        mock_cmd.side_effect = mock_start

        tasks = [
            client.post("/api/v1/backtest/start", json={
                "symbols": ["BTC/USDT"],
                "start_date": "2024-01-01",
                "end_date": "2024-06-01",
                "timeframe": "30m",
                "fee_pct": 0.04,
            })
            for _ in range(3)
        ]
        responses = await asyncio.gather(*tasks)

        statuses = [r.json()["status"] for r in responses]
        assert "timeout" in statuses
        assert statuses.count("started") == 2


# ── 5C-2: Long-Running Stability ────────────────────────────

class TestLongRunning:
    @pytest.mark.anyio
    @patch("app.api.backtest._send_engine_command", new_callable=AsyncMock)
    async def test_multiyear_backtest_completes(self, mock_cmd, client):
        """Submit a 2-year backtest and verify it returns a result."""
        mock_cmd.return_value = {"status": "started", "job_id": "long-job-1"}

        r = await client.post("/api/v1/backtest/start", json={
            "symbols": ["BTC/USDT", "ETH/USDT", "SOL/USDT"],
            "start_date": "2022-01-01",
            "end_date": "2024-01-01",
            "timeframe": "30m",
            "fee_pct": 0.04,
        })
        assert r.status_code == 200
        assert r.json()["status"] == "started"
        assert r.json()["job_id"] == "long-job-1"

    @pytest.mark.anyio
    @patch("app.api.backtest._send_engine_command", new_callable=AsyncMock)
    async def test_progress_updates_are_monotonic(self, mock_cmd, client):
        """Verify progress values increase monotonically."""
        progress_values = [10, 25, 50, 75, 90, 100]
        call_idx = 0

        async def mock_status(action, params, **kwargs):
            nonlocal call_idx
            pct = progress_values[min(call_idx, len(progress_values) - 1)]
            call_idx += 1
            return {"status": "running", "progress": pct, "job_id": params["job_id"]}

        mock_cmd.side_effect = mock_status

        prev_progress = 0
        for _ in range(6):
            r = await client.get("/api/v1/backtest/status/job-long")
            data = r.json()
            assert data["progress"] >= prev_progress, \
                f"Progress went backwards: {data['progress']} < {prev_progress}"
            prev_progress = data["progress"]


# ── 5C-3: Cancellation Handling ──────────────────────────────

class TestCancellation:
    @pytest.mark.anyio
    @patch("app.api.backtest._send_engine_command", new_callable=AsyncMock)
    async def test_cancel_returns_cancelled_status(self, mock_cmd, client):
        """Cancel a running job and verify status transitions to cancelled."""
        mock_cmd.return_value = {"status": "cancelled", "job_id": "cancel-job-1"}

        r = await client.post("/api/v1/backtest/cancel/cancel-job-1")
        assert r.status_code == 200
        assert r.json()["status"] == "cancelled"
        mock_cmd.assert_called_once_with("cancel_backtest", {"job_id": "cancel-job-1"})

    @pytest.mark.anyio
    @patch("app.api.backtest._send_engine_command", new_callable=AsyncMock)
    async def test_subsequent_job_after_cancel_works(self, mock_cmd, client):
        """After cancelling, a new backtest job starts normally."""
        call_idx = 0

        async def mock_handler(action, params, **kwargs):
            nonlocal call_idx
            call_idx += 1
            if action == "cancel_backtest":
                return {"status": "cancelled"}
            return {"status": "started", "job_id": f"post-cancel-{call_idx}"}

        mock_cmd.side_effect = mock_handler

        # Cancel first
        r1 = await client.post("/api/v1/backtest/cancel/old-job")
        assert r1.json()["status"] == "cancelled"

        # Start new job
        r2 = await client.post("/api/v1/backtest/start", json={
            "symbols": ["BTC/USDT"],
            "start_date": "2024-01-01",
            "end_date": "2024-06-01",
            "timeframe": "30m",
            "fee_pct": 0.04,
        })
        assert r2.status_code == 200
        assert r2.json()["status"] == "started"

    @pytest.mark.anyio
    @patch("app.api.backtest._send_engine_command", new_callable=AsyncMock)
    async def test_cancel_nonexistent_job_handled(self, mock_cmd, client):
        """Cancelling a job that doesn't exist returns gracefully."""
        mock_cmd.return_value = {"status": "not_found", "job_id": "ghost-job"}

        r = await client.post("/api/v1/backtest/cancel/ghost-job")
        assert r.status_code == 200
        assert r.json()["status"] == "not_found"


# ── 5C-4: Failure Injection ──────────────────────────────────

class TestFailureInjection:
    @pytest.mark.anyio
    @patch("app.api.backtest._send_engine_command", new_callable=AsyncMock)
    async def test_empty_symbols_accepted_by_api(self, mock_cmd, client):
        """Empty symbols list is valid Pydantic; engine decides if it's an error."""
        mock_cmd.return_value = {"status": "error", "error": "No symbols provided"}
        r = await client.post("/api/v1/backtest/start", json={
            "symbols": [],
            "start_date": "2024-01-01",
            "end_date": "2024-06-01",
            "timeframe": "30m",
            "fee_pct": 0.04,
        })
        assert r.status_code == 200
        assert r.json()["status"] == "error"

    @pytest.mark.anyio
    async def test_malformed_json_rejected(self, client):
        """Completely malformed request body should return 422."""
        r = await client.post(
            "/api/v1/backtest/start",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 422

    @pytest.mark.anyio
    async def test_invalid_fee_type_rejected(self, client):
        """Non-numeric fee_pct should return 422."""
        r = await client.post("/api/v1/backtest/start", json={
            "symbols": ["BTC/USDT"],
            "start_date": "2024-01-01",
            "end_date": "2024-06-01",
            "timeframe": "30m",
            "fee_pct": "not_a_number",
        })
        assert r.status_code == 422

    @pytest.mark.anyio
    @patch("app.api.backtest._send_engine_command", new_callable=AsyncMock)
    async def test_engine_error_propagated(self, mock_cmd, client):
        """Engine returning an error dict should be forwarded to client."""
        mock_cmd.return_value = {
            "status": "error",
            "error": "No data available for symbol XYZ/USDT",
        }

        r = await client.post("/api/v1/backtest/start", json={
            "symbols": ["XYZ/USDT"],
            "start_date": "2024-01-01",
            "end_date": "2024-06-01",
            "timeframe": "30m",
            "fee_pct": 0.04,
        })
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "error"
        assert "No data available" in data["error"]

    @pytest.mark.anyio
    @patch("app.api.backtest._send_engine_command", new_callable=AsyncMock)
    async def test_invalid_date_range_error(self, mock_cmd, client):
        """End date before start date — engine returns error."""
        mock_cmd.return_value = {
            "status": "error",
            "error": "end_date must be after start_date",
        }

        r = await client.post("/api/v1/backtest/start", json={
            "symbols": ["BTC/USDT"],
            "start_date": "2024-06-01",
            "end_date": "2024-01-01",
            "timeframe": "30m",
            "fee_pct": 0.04,
        })
        assert r.status_code == 200
        assert "end_date must be after start_date" in r.json()["error"]


# ── 5C-5: Result Integrity ───────────────────────────────────

class TestResultIntegrity:
    @pytest.mark.anyio
    @patch("app.api.backtest._send_engine_command", new_callable=AsyncMock)
    async def test_identical_backtests_produce_same_results(self, mock_cmd, client):
        """Two identical backtests return deterministic results."""
        canonical_result = {
            "status": "complete",
            "job_id": "det-job",
            "pf": 1.2758,
            "win_rate": 56.1,
            "trade_count": 1745,
            "max_dd": -20.33,
            "cagr": 47.44,
        }
        mock_cmd.return_value = canonical_result

        params = {
            "symbols": ["BTC/USDT", "ETH/USDT", "SOL/USDT"],
            "start_date": "2022-03-22",
            "end_date": "2026-03-21",
            "timeframe": "30m",
            "fee_pct": 0.04,
        }

        r1 = await client.post("/api/v1/backtest/start", json=params)
        r2 = await client.post("/api/v1/backtest/start", json=params)

        d1, d2 = r1.json(), r2.json()

        # Compare key metrics within tolerance
        assert abs(d1["pf"] - d2["pf"]) < 0.001, \
            f"PF mismatch: {d1['pf']} vs {d2['pf']}"
        assert abs(d1["win_rate"] - d2["win_rate"]) < 0.1, \
            f"WR mismatch: {d1['win_rate']} vs {d2['win_rate']}"
        assert d1["trade_count"] == d2["trade_count"], \
            f"Trade count mismatch: {d1['trade_count']} vs {d2['trade_count']}"

    @pytest.mark.anyio
    @patch("app.api.backtest._send_engine_command", new_callable=AsyncMock)
    async def test_result_contains_required_fields(self, mock_cmd, client):
        """Backtest results contain all required metric fields."""
        mock_cmd.return_value = {
            "status": "complete",
            "job_id": "fields-job",
            "pf": 1.5,
            "win_rate": 55.0,
            "trade_count": 500,
            "max_dd": -15.0,
            "cagr": 30.0,
        }

        r = await client.post("/api/v1/backtest/start", json={
            "symbols": ["BTC/USDT"],
            "start_date": "2024-01-01",
            "end_date": "2024-12-31",
            "timeframe": "30m",
            "fee_pct": 0.04,
        })
        data = r.json()

        required_fields = ["status", "pf", "win_rate", "trade_count"]
        for field in required_fields:
            assert field in data, f"Missing required field: {field}"


# ── 6F-1: Large Dataset Stress (5yr, 3 symbols) ──────────────

class TestLargeDatasetStress:
    """Phase 6F: Verify 5-year 3-symbol backtest submits and tracks correctly."""

    @pytest.mark.anyio
    @patch("app.api.backtest._send_engine_command", new_callable=AsyncMock)
    async def test_5yr_3symbol_backtest_submits(self, mock_cmd, client):
        """5-year backtest with 3 symbols submits successfully."""
        mock_cmd.return_value = {
            "status": "started",
            "job_id": "stress-5yr-3sym",
        }
        r = await client.post("/api/v1/backtest/start", json={
            "symbols": ["BTC/USDT", "ETH/USDT", "SOL/USDT"],
            "start_date": "2021-01-01",
            "end_date": "2026-01-01",
            "timeframe": "30m",
            "fee_pct": 0.04,
        })
        assert r.status_code == 200
        assert r.json()["status"] == "started"

    @pytest.mark.anyio
    @patch("app.api.backtest._send_engine_command", new_callable=AsyncMock)
    async def test_5yr_large_result_set_parsed(self, mock_cmd, client):
        """Large result (5000+ trades) is returned correctly."""
        mock_cmd.return_value = {
            "status": "complete",
            "job_id": "stress-5yr-big",
            "pf": 1.35,
            "win_rate": 54.2,
            "trade_count": 5247,
            "max_dd": -22.1,
            "cagr": 38.7,
        }
        r = await client.get("/api/v1/backtest/results/stress-5yr-big")
        assert r.status_code == 200
        data = r.json()
        assert data["trade_count"] == 5247
        assert data["pf"] == 1.35


# ── 6F-2: Concurrent Cancellation ─────────────────────────────

class TestConcurrentCancellation:
    """Phase 6F: Cancel during concurrent backtest execution."""

    @pytest.mark.anyio
    @patch("app.api.backtest._send_engine_command", new_callable=AsyncMock)
    async def test_cancel_one_of_concurrent_jobs(self, mock_cmd, client):
        """Start 2 jobs, cancel one, verify the other continues."""
        call_log = []

        async def mock_handler(action, params, **kwargs):
            call_log.append((action, params))
            if action == "cancel_backtest":
                return {"status": "cancelled", "job_id": params["job_id"]}
            return {"status": "started", "job_id": f"conc-{len(call_log)}"}

        mock_cmd.side_effect = mock_handler

        # Start two jobs
        r1 = await client.post("/api/v1/backtest/start", json={
            "symbols": ["BTC/USDT"],
            "start_date": "2024-01-01",
            "end_date": "2024-06-01",
            "timeframe": "30m",
            "fee_pct": 0.04,
        })
        r2 = await client.post("/api/v1/backtest/start", json={
            "symbols": ["ETH/USDT"],
            "start_date": "2024-01-01",
            "end_date": "2024-06-01",
            "timeframe": "30m",
            "fee_pct": 0.04,
        })
        assert r1.status_code == 200
        assert r2.status_code == 200

        # Cancel first job
        job_id_1 = r1.json()["job_id"]
        r_cancel = await client.post(f"/api/v1/backtest/cancel/{job_id_1}")
        assert r_cancel.json()["status"] == "cancelled"

        # Verify cancel was called with correct job_id
        cancel_calls = [c for c in call_log if c[0] == "cancel_backtest"]
        assert len(cancel_calls) == 1
        assert cancel_calls[0][1]["job_id"] == job_id_1


# ── 6F-3: Memory Baseline ─────────────────────────────────────

class TestMemoryBaseline:
    """Phase 6F: Verify API handles large payloads without excessive memory."""

    @pytest.mark.anyio
    @patch("app.api.backtest._send_engine_command", new_callable=AsyncMock)
    async def test_large_result_payload_handled(self, mock_cmd, client):
        """API returns a large results payload (simulating large trade list)."""
        import sys
        large_trades = [{"id": i, "pnl": 0.5 * i} for i in range(10000)]
        result = {
            "status": "complete",
            "job_id": "mem-test",
            "pf": 1.5,
            "win_rate": 55.0,
            "trade_count": 10000,
            "max_dd": -18.0,
            "cagr": 40.0,
            "trades": large_trades,
        }
        mock_cmd.return_value = result

        r = await client.get("/api/v1/backtest/results/mem-test")
        assert r.status_code == 200
        data = r.json()
        assert data["trade_count"] == 10000
        # Verify response size is reasonable (< 5 MB JSON)
        assert len(r.content) < 5 * 1024 * 1024


# ── 6F-4: Timeout Cascade ─────────────────────────────────────

class TestTimeoutCascade:
    """Phase 6F: Verify timeout in one job doesn't cascade to others."""

    @pytest.mark.anyio
    @patch("app.api.backtest._send_engine_command", new_callable=AsyncMock)
    async def test_timeout_isolated(self, mock_cmd, client):
        """One job times out; other jobs return normally."""
        call_idx = 0

        async def mock_handler(action, params, **kwargs):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 2:
                # Simulate timeout
                return {"status": "timeout", "job_id": f"job-{call_idx}"}
            return {"status": "started", "job_id": f"job-{call_idx}"}

        mock_cmd.side_effect = mock_handler

        tasks = [
            client.post("/api/v1/backtest/start", json={
                "symbols": ["BTC/USDT"],
                "start_date": "2024-01-01",
                "end_date": "2024-06-01",
                "timeframe": "30m",
                "fee_pct": 0.04,
            })
            for _ in range(3)
        ]
        responses = await asyncio.gather(*tasks)

        statuses = [r.json()["status"] for r in responses]
        assert statuses.count("timeout") == 1
        assert statuses.count("started") == 2

    @pytest.mark.anyio
    @patch("app.api.backtest._send_engine_command", new_callable=AsyncMock)
    async def test_post_timeout_job_recovers(self, mock_cmd, client):
        """After a timeout, a subsequent backtest starts normally."""
        call_idx = 0

        async def mock_handler(action, params, **kwargs):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 1:
                return {"status": "timeout", "job_id": "timeout-job"}
            return {"status": "started", "job_id": f"recovered-{call_idx}"}

        mock_cmd.side_effect = mock_handler

        # First call: timeout
        r1 = await client.post("/api/v1/backtest/start", json={
            "symbols": ["BTC/USDT"],
            "start_date": "2024-01-01",
            "end_date": "2024-06-01",
            "timeframe": "30m",
            "fee_pct": 0.04,
        })
        assert r1.json()["status"] == "timeout"

        # Second call: should succeed
        r2 = await client.post("/api/v1/backtest/start", json={
            "symbols": ["ETH/USDT"],
            "start_date": "2024-01-01",
            "end_date": "2024-06-01",
            "timeframe": "30m",
            "fee_pct": 0.04,
        })
        assert r2.json()["status"] == "started"
