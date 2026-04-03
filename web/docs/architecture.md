# NexusTrader Web — Architecture Summary

## System Overview

NexusTrader Web is a full-stack web application that provides a browser-based interface to the NexusTrader algorithmic trading engine. It replaces the original PySide6 desktop GUI with a modern React frontend backed by FastAPI.

## Technology Stack

| Layer | Technology | Version |
|-------|-----------|---------|
| Frontend | React + TypeScript + Vite | React 19, Vite 6 |
| State Management | React Query (TanStack Query) | v5 |
| Routing | React Router | v7 |
| Backend API | FastAPI + Uvicorn | 0.115+ |
| Database | PostgreSQL | 16 |
| Cache/PubSub | Redis | 7 |
| Real-time | WebSocket (native FastAPI) | — |
| Auth | JWT (HS256) + Cloudflare Access | PyJWT |
| Containerization | Docker + Docker Compose | v3.9 |
| E2E Testing | Playwright | Latest |
| CI | GitHub Actions | — |

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                    Cloudflare Tunnel                          │
│  (Zero Trust Access — JWT validation on every request)       │
└──────────────────────┬──────────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────────┐
│                  Nginx (port 3000)                            │
│  Static files + /api proxy → :8000 + /ws upgrade             │
└──────────┬───────────────────────────┬──────────────────────┘
           │                           │
┌──────────▼──────────┐  ┌────────────▼─────────────────────┐
│   React Frontend     │  │     FastAPI Backend (port 8000)   │
│                      │  │                                    │
│  - 18 pages          │  │  Middleware Stack:                 │
│  - ErrorBoundary     │  │  1. AuditMiddleware (X-Request-ID) │
│  - Skeleton loading  │  │  2. SecurityHeaders (HSTS, CSP)   │
│  - API client w/     │  │  3. CORS                          │
│    retry + timeout   │  │  4. RateLimit (slowapi)           │
│  - WebSocket mgr     │  │  5. CloudflareAccess              │
│                      │  │                                    │
│  Routes:             │  │  15 API routers at /api/v1/        │
│  /login, /setup      │  │  WebSocket at /ws                  │
│  /dashboard          │  │                                    │
│  /scanner            │  │  Auth: JWT access (15m) + refresh  │
│  /trading            │  │  (7d) + account lockout (5/15min)  │
│  /charts             │  │                                    │
│  /analytics/*        │  │  Structured JSON logging           │
│  /risk               │  │  Sensitive data masking            │
│  /settings           │  │                                    │
│  /backtest           │  └──────┬────────────┬───────────────┘
│  /validation         │         │            │
│  /intelligence       │   ┌─────▼─────┐ ┌───▼──────┐
│  /logs               │   │PostgreSQL │ │  Redis   │
│  /*  (404)           │   │  (5432)   │ │  (6379)  │
└──────────────────────┘   └───────────┘ └──────────┘
```

## Security Architecture

1. **Cloudflare Access** (outer gate): RS256 JWT validation on all non-health requests. Disabled in dev mode.
2. **Application JWT** (inner gate): HS256 access tokens (15min TTL) + refresh tokens (7 day, hashed in DB).
3. **Account Lockout**: 5 failed login attempts → 15 minute lockout (423 LOCKED).
4. **Password Complexity**: 12+ chars, uppercase, lowercase, digit, special character.
5. **Security Headers**: X-Content-Type-Options, X-Frame-Options, HSTS, CSP, Referrer-Policy.
6. **Rate Limiting**: Global (100/min), Auth (5/min), Commands (10/min).
7. **WebSocket Security**: Cryptographic connection IDs, per-client rate limiting (20 msg/sec), 64KB message size cap.
8. **Structured Logging**: JSON format with automatic JWT/password/API key masking.

## Data Flow

```
User Action → React → API Client (retry + timeout) → FastAPI Router
  → Auth Dependency (JWT validation) → Business Logic → PostgreSQL/Redis
  → JSON Response → React Query Cache → UI Update

Real-time: FastAPI WS → ConnectionManager → Per-channel broadcast → React WS hook
```

## Test Architecture

| Category | Tool | Count | Infrastructure |
|----------|------|-------|----------------|
| Unit | pytest | ~350 | None |
| Sandbox Integration | pytest + asyncio | ~10 | In-memory |
| Docker Integration | pytest | ~5 | PostgreSQL + Redis |
| E2E Browser | Playwright | ~40 | Full Docker stack |
| Stress | pytest | 21 | Varies |

## File Structure

```
web/
├── backend/
│   ├── main.py              # FastAPI app entry point
│   ├── app/
│   │   ├── api/             # 15 route modules
│   │   ├── auth/            # JWT + dependencies
│   │   ├── middleware/       # Audit, CloudflareAccess, SecurityHeaders, RateLimit
│   │   ├── ws/              # WebSocket manager + routes
│   │   ├── models/          # SQLAlchemy ORM models
│   │   ├── database.py      # Async session factory
│   │   ├── config.py        # Settings + validation
│   │   └── logging_config.py # Structured JSON logging
│   ├── tests/               # 20 test modules
│   └── Dockerfile
├── frontend/
│   ├── src/
│   │   ├── pages/           # 18 page components
│   │   ├── components/      # Shared components (ErrorBoundary, Skeleton, etc.)
│   │   ├── api/client.ts    # Hardened API client
│   │   ├── hooks/           # Custom React hooks
│   │   └── App.tsx          # Root with routing
│   ├── e2e/                 # 19 Playwright spec files
│   └── Dockerfile
├── infra/
│   ├── cloudflared/         # Tunnel config
│   ├── nginx/               # Production reverse proxy
│   └── scripts/             # Deployment scripts
├── docker-compose.yml       # Production stack
├── docker-compose.e2e.yml   # E2E test stack
└── .env.example             # Environment template
```
