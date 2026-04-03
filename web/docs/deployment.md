# NexusTrader Web — Deployment Guide

## Prerequisites

- Docker Desktop 4.x+ (or Docker Engine 24+ with Compose v2)
- A domain name with DNS managed by Cloudflare (for production)
- cloudflared CLI (for Cloudflare Tunnel)

## Quick Start (Development)

```bash
cd web/

# Copy environment template
cp .env.example .env

# Start all services
docker compose up -d

# Check health
curl http://localhost:8000/health

# View logs
docker compose logs -f api
```

The API will be available at `http://localhost:8000` and the database at `localhost:5432`.

## Production Deployment

### 1. Configure Environment

```bash
cp .env.example .env
```

Edit `.env` with production values:

```env
POSTGRES_PASSWORD=<strong-random-password>
NEXUS_JWT_SECRET=<64-char-random-string>
NEXUS_ENCRYPTION_KEY=<32-char-random-string>
NEXUS_DEBUG=false
NEXUS_CORS_ORIGINS=https://nexustrader.yourdomain.com
NEXUS_LOG_FORMAT=json
NEXUS_LOG_LEVEL=INFO
```

Generate secrets:
```bash
python -c "import secrets; print(secrets.token_hex(32))"  # JWT secret
python -c "import secrets; print(secrets.token_hex(16))"  # Encryption key
```

### 2. Build and Start

```bash
# Using the deployment script
chmod +x infra/scripts/deploy.sh
./infra/scripts/deploy.sh

# Or manually
docker compose build --no-cache
docker compose up -d
```

### 3. Build Frontend for Production

```bash
cd frontend/
npm ci
npm run build
# Output in frontend/dist/ — serve via nginx
```

### 4. Configure Nginx

Copy `infra/nginx/nginx.conf` to your nginx configuration. The nginx container should serve the built frontend from `frontend/dist/` and proxy API requests to the backend.

### 5. Set Up Cloudflare Tunnel

```bash
# Authenticate with Cloudflare
cloudflared tunnel login

# Create tunnel
cloudflared tunnel create nexustrader

# Route DNS
cloudflared tunnel route dns nexustrader nexustrader.yourdomain.com

# Edit infra/cloudflared/config.yml with your domain

# Start tunnel
cloudflared tunnel --config infra/cloudflared/config.yml run nexustrader
```

### 6. Enable Cloudflare Access

1. Go to Cloudflare Zero Trust dashboard
2. Create an Access Application for `nexustrader.yourdomain.com`
3. Configure authentication policies (email, SSO, etc.)
4. Note the Application Audience Tag
5. Update `.env`:
   ```env
   NEXUS_CF_ENABLED=true
   NEXUS_CF_TEAM_DOMAIN=yourteam.cloudflareaccess.com
   NEXUS_CF_AUDIENCE=<application-audience-tag>
   ```
6. Restart the API: `docker compose restart api`

### 7. First-Run Setup

Navigate to `https://nexustrader.yourdomain.com/setup` and create the initial admin account.

## Verification

```bash
# Health check
curl https://nexustrader.yourdomain.com/api/v1/health

# Security headers check
curl -I https://nexustrader.yourdomain.com/api/v1/health

# Expected headers:
#   X-Content-Type-Options: nosniff
#   X-Frame-Options: DENY
#   Strict-Transport-Security: max-age=31536000; includeSubDomains
#   Content-Security-Policy: default-src 'self'
```

## Updating

```bash
cd web/
git pull
docker compose build
docker compose up -d
```

## Backup

```bash
# Database backup
docker compose exec postgres pg_dump -U nexus nexustrader > backup_$(date +%Y%m%d).sql

# Restore
docker compose exec -T postgres psql -U nexus nexustrader < backup_20260403.sql
```
