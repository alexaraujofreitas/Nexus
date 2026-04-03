# NexusTrader Web Backend — API Routers (Phase 2A)

## Overview

Seven new API router modules have been created to expose NexusTrader Trading Engine functionality via REST endpoints. All endpoints require JWT authentication and communicate with the Trading Engine via Redis request/reply pattern.

## Router Files

### 1. Dashboard Router (`app/api/dashboard.py`)

Aggregated portfolio and system state snapshots.

**Endpoints:**

- `GET /api/v1/dashboard/summary`
  - Returns: aggregated portfolio state, crash defense status, engine state
  - Params: none
  - Action: `get_dashboard`

- `GET /api/v1/dashboard/crash-defense`
  - Returns: detailed crash defense tier, score, actions log
  - Params: none
  - Action: `get_crash_defense`

### 2. Scanner Router (`app/api/scanner.py`)

IDSS scanner control and results.

**Endpoints:**

- `GET /api/v1/scanner/results`
  - Returns: results from most recent scan cycle
  - Params: none
  - Action: `get_scanner_results`

- `GET /api/v1/scanner/watchlist`
  - Returns: current scanner watchlist with symbol weights
  - Params: none
  - Action: `get_watchlist`

- `POST /api/v1/scanner/trigger`
  - Returns: confirmation of scan trigger
  - Params: none
  - Action: `trigger_scan`

### 3. Signals Router (`app/api/signals.py`)

Intelligence agents and confluence signals.

**Endpoints:**

- `GET /api/v1/signals/agents`
  - Returns: status of all 23 intelligence agents
  - Params: none
  - Action: `get_agent_status`

- `GET /api/v1/signals/confluence`
  - Returns: recent confluence signals from signal pipeline
  - Params: none
  - Action: `get_signals`

### 4. Risk Router (`app/api/risk.py`)

Portfolio risk metrics and circuit breakers.

**Endpoints:**

- `GET /api/v1/risk/status`
  - Returns: portfolio heat, drawdown, circuit breaker status, crash tier
  - Params: none
  - Action: `get_risk_status`

### 5. Trades Router (`app/api/trades.py`)

Trade history and outcomes.

**Endpoints:**

- `GET /api/v1/trades/history`
  - Returns: paginated trade history with details
  - Query Params:
    - `page` (int, default=1): page number (≥1)
    - `per_page` (int, default=50): items per page (1–200)
  - Action: `get_trade_history`

### 6. System Router (`app/api/system.py`)

System health and emergency controls.

**Endpoints:**

- `GET /api/v1/system/health`
  - Returns: detailed system health (threads, scanner, executor, exchange, engine)
  - Params: none
  - Action: `get_system_health`

- `POST /api/v1/system/kill-switch`
  - Returns: confirmation of emergency stop
  - Params: none
  - Action: `kill_switch`
  - Note: EMERGENCY endpoint — closes all positions, pauses trading, stops scanner

### 7. Settings Router (`app/api/settings_api.py`)

Runtime configuration management.

**Endpoints:**

- `GET /api/v1/settings/`
  - Returns: runtime configuration (full or by section)
  - Query Params:
    - `section` (str, optional): specific config section to retrieve
  - Action: `get_config`

- `PATCH /api/v1/settings/`
  - Returns: confirmation of config update
  - Request Body:
    ```json
    {
      "updates": {
        "key.path": value,
        ...
      }
    }
    ```
  - Action: `update_config`

## Authentication

All endpoints require a valid JWT bearer token in the `Authorization` header:

```
Authorization: Bearer <jwt_token>
```

Authentication is enforced via the `get_current_user` dependency on all routers.

## Redis Command Pattern

All endpoints use the `_send_engine_command()` helper from `app/api/engine.py`:

1. Generate unique `command_id`
2. Set idempotency key in Redis (1h TTL)
3. RPUSH command JSON to `nexus:engine:commands` queue
4. BLPOP reply from `nexus:engine:replies:{command_id}` (10s timeout)
5. Return JSON response

**Command Schema:**
```json
{
  "command_id": "<uuid>",
  "action": "<action_name>",
  "params": { ... }
}
```

## Allowed Actions

The following actions are registered in `app/api/engine.py` and allowed via the `/api/v1/engine/command` endpoint:

**Original (v1.0):**
- `start_scanner`, `stop_scanner`
- `pause_trading`, `resume_trading`
- `close_position`, `close_all_positions`
- `refresh_data`
- `get_positions`, `get_portfolio`, `get_config`

**Phase 2A Additions:**
- `get_dashboard`, `get_crash_defense`
- `get_scanner_results`, `get_watchlist`, `trigger_scan`
- `get_agent_status`, `get_signals`
- `get_risk_status`
- `get_trade_history`
- `update_config`
- `get_system_health`, `kill_switch`

## Error Handling

All endpoints return JSON responses with the following structure on success:

```json
{
  "status": "ok",
  "data": { ... }
}
```

On timeout or error:

```json
{
  "status": "timeout|error",
  "command_id": "<uuid>",
  "error": "..."
}
```

HTTP Status Codes:
- `200 OK`: command executed successfully
- `400 Bad Request`: invalid action name
- `401 Unauthorized`: missing/invalid JWT
- `500 Internal Server Error`: Redis connection or engine failure

## Usage Examples

### Fetch Dashboard Summary

```bash
curl -X GET http://localhost:8000/api/v1/dashboard/summary \
  -H "Authorization: Bearer $JWT_TOKEN"
```

### Trigger Scanner

```bash
curl -X POST http://localhost:8000/api/v1/scanner/trigger \
  -H "Authorization: Bearer $JWT_TOKEN"
```

### Get Paginated Trade History

```bash
curl -X GET "http://localhost:8000/api/v1/trades/history?page=1&per_page=25" \
  -H "Authorization: Bearer $JWT_TOKEN"
```

### Update Configuration

```bash
curl -X PATCH http://localhost:8000/api/v1/settings/ \
  -H "Authorization: Bearer $JWT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "updates": {
      "risk_engine.risk_pct_per_trade": 0.75,
      "scanner.auto_execute": true
    }
  }'
```

### Emergency Kill-Switch

```bash
curl -X POST http://localhost:8000/api/v1/system/kill-switch \
  -H "Authorization: Bearer $JWT_TOKEN"
```

## Integration Notes

1. **Router Registration**: All 7 routers are imported and registered in `main.py` with the `/api/v1` prefix.

2. **Settings Router Naming**: Named `settings_api.py` to avoid naming conflict with the core `config` module.

3. **Command Validation**: All actions are validated against the `allowed_actions` set in `engine.py` before sending to Trading Engine.

4. **Idempotency**: All commands are deduplicated via Redis key (`nexus:cmd:idem:{command_id}`) with 1-hour TTL.

5. **Timeouts**: Default timeout is 10 seconds. Adjust in `_send_engine_command()` if needed.

## Files Modified/Created

**Created:**
- `app/api/dashboard.py`
- `app/api/scanner.py`
- `app/api/signals.py`
- `app/api/risk.py`
- `app/api/trades.py`
- `app/api/system.py`
- `app/api/settings_api.py`

**Modified:**
- `app/api/engine.py` (extended `allowed_actions` set)
- `main.py` (added router imports and registration)
