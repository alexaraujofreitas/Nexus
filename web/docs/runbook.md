# NexusTrader Web — Operations Runbook

## Service Management

### Start All Services
```bash
cd web/
docker compose up -d
```

### Stop All Services
```bash
docker compose down       # Stop containers (keep data)
docker compose down -v    # Stop + delete volumes (DESTRUCTIVE)
```

### Restart a Single Service
```bash
docker compose restart api       # Restart API only
docker compose restart postgres  # Restart database only
docker compose restart redis     # Restart Redis only
```

### View Logs
```bash
docker compose logs -f api        # API logs (follow)
docker compose logs -f --tail=100 # Last 100 lines, all services
docker compose logs postgres      # Database logs
```

## Health Checks

### API Health
```bash
curl -s http://localhost:8000/health | python -m json.tool
```
Expected: `{"status": "ok", ...}`

### Database Health
```bash
docker compose exec postgres pg_isready -U nexus -d nexustrader
```

### Redis Health
```bash
docker compose exec redis redis-cli ping
```
Expected: `PONG`

### Full Stack Check
```bash
# All services running
docker compose ps

# All health checks passing
docker compose ps --format "table {{.Name}}\t{{.Status}}"
```

## Troubleshooting

### API Returns 500
1. Check API logs: `docker compose logs -f api`
2. Verify database connection: `docker compose exec postgres pg_isready -U nexus`
3. Verify Redis: `docker compose exec redis redis-cli ping`
4. Check for missing environment variables in `.env`

### Database Connection Refused
1. Check if postgres container is running: `docker compose ps postgres`
2. Check postgres logs: `docker compose logs postgres`
3. Verify NEXUS_DATABASE_URL in API environment
4. Restart: `docker compose restart postgres`

### WebSocket Not Connecting
1. Verify API is healthy: `curl http://localhost:8000/health`
2. Check nginx WebSocket proxy config (must have `Upgrade` and `Connection` headers)
3. Check browser console for CORS errors
4. Verify NEXUS_CORS_ORIGINS includes the frontend domain

### Cloudflare Access 401 Errors
1. Verify `NEXUS_CF_ENABLED=true` in API environment
2. Check `NEXUS_CF_TEAM_DOMAIN` matches your Cloudflare team
3. Verify `NEXUS_CF_AUDIENCE` matches the Access application
4. Check API logs for specific JWT validation error
5. Force-refresh signing keys: restart the API container

### Account Locked (423 Response)
The account locks after 5 failed login attempts for 15 minutes.
- Wait 15 minutes, or
- Reset via database:
  ```bash
  docker compose exec postgres psql -U nexus -d nexustrader \
    -c "UPDATE users SET failed_login_attempts=0, locked_until=NULL WHERE email='user@example.com';"
  ```

### Rate Limited (429 Response)
Default limits: 100 req/min global, 5 req/min auth endpoints.
- Wait for the rate limit window to expire
- Adjust limits via environment variables if needed:
  ```env
  NEXUS_RATE_LIMIT_GLOBAL=200
  NEXUS_RATE_LIMIT_AUTH=10
  ```

### Frontend Build Fails
1. Verify Node.js 20+: `node --version`
2. Clean install: `rm -rf node_modules && npm ci`
3. Check TypeScript: `npx tsc --noEmit`
4. Check for missing environment: `VITE_API_BASE_URL` must be set

## Monitoring

### Key Metrics to Watch
- API response times (p95 should be < 500ms)
- WebSocket connection count
- Database connection pool utilization
- Redis memory usage
- Error rate in structured logs

### Log Analysis (JSON format)
```bash
# Find all errors
docker compose logs api 2>&1 | grep '"level":"ERROR"'

# Find slow requests (>1s)
docker compose logs api 2>&1 | grep '"duration_ms"' | python -c "
import sys, json
for line in sys.stdin:
    try:
        d = json.loads(line)
        if d.get('duration_ms', 0) > 1000:
            print(f'{d[\"method\"]} {d[\"path\"]} - {d[\"duration_ms\"]}ms')
    except: pass
"
```

## Backup and Recovery

### Automated Daily Backup
```bash
# Add to crontab
0 2 * * * cd /path/to/web && docker compose exec -T postgres pg_dump -U nexus nexustrader | gzip > /backups/nexus_$(date +\%Y\%m\%d).sql.gz
```

### Point-in-Time Recovery
```bash
# Stop API to prevent writes
docker compose stop api

# Restore from backup
gunzip -c /backups/nexus_20260403.sql.gz | docker compose exec -T postgres psql -U nexus nexustrader

# Restart
docker compose start api
```
