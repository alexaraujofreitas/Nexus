# ============================================================
# NEXUS TRADER Web — FastAPI Application Entry Point
#
# Initializes Qt shim (for core/ imports), configures middleware,
# mounts routers, and manages lifecycle events.
#
# Run: uvicorn main:app --host 0.0.0.0 --port 8000 --reload
# ============================================================
from __future__ import annotations

import logging
import sys
import os
import uuid

# ── Path setup ──────────────────────────────────────────────
# Add backend dir so `app`, `core_patch` are importable
_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

# Add NexusTrader project root so `core`, `config` are importable
_PROJECT_ROOT = os.path.abspath(os.path.join(_BACKEND_DIR, "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# ── Install Qt shim BEFORE any core/ imports ────────────────
from core_patch import install_qt_shim  # noqa: E402
install_qt_shim()

# ── Now safe to import FastAPI and app modules ──────────────
from contextlib import asynccontextmanager  # noqa: E402

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from app.config import get_settings, validate_settings, ConfigurationError  # noqa: E402
from app.database import dispose_async_engine, get_async_session_factory, init_async_engine  # noqa: E402
from app.middleware.cloudflare import CloudflareAccessMiddleware  # noqa: E402
from app.middleware.security_headers import SecurityHeadersMiddleware  # noqa: E402
from app.middleware.rate_limit import limiter, rate_limit_exceeded_handler  # noqa: E402
from app.middleware.audit import AuditMiddleware  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402
from slowapi import _rate_limit_exceeded_handler  # noqa: E402
from slowapi.errors import RateLimitExceeded  # noqa: E402
from app.api.health import router as health_router  # noqa: E402
from app.api.auth import router as auth_router  # noqa: E402
from app.api.engine import router as engine_router  # noqa: E402
from app.api.dashboard import router as dashboard_router  # noqa: E402
from app.api.scanner import router as scanner_router  # noqa: E402
from app.api.signals import router as signals_router  # noqa: E402
from app.api.risk import router as risk_router  # noqa: E402
from app.api.trades import router as trades_router  # noqa: E402
from app.api.system import router as system_router  # noqa: E402
from app.api.settings_api import router as settings_router  # noqa: E402
from app.api.charts import router as charts_router  # noqa: E402
from app.api.trading import router as trading_router  # noqa: E402
from app.api.logs import router as logs_router  # noqa: E402
from app.api.analytics import router as analytics_router  # noqa: E402
from app.api.backtest import router as backtest_router  # noqa: E402
from app.api.validation import router as validation_router  # noqa: E402
from app.api.vault import router as vault_router  # noqa: E402
from app.api.exchanges import router as exchanges_router  # noqa: E402
from app.api.monitor import router as monitor_router  # noqa: E402
from app.api.market_data import router as market_data_router  # noqa: E402
from app.ws.routes import router as ws_router  # noqa: E402
from app.ws.manager import ws_manager  # noqa: E402

# ── Logging ─────────────────────────────────────────────────
configure_logging()
logger = logging.getLogger("nexus.api")


# ── Lifecycle ───────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle manager."""
    logger.info("Starting NexusTrader API service")

    # Validate configuration before proceeding
    config_errors = validate_settings(settings)
    if config_errors:
        for err in config_errors:
            logger.error("Configuration error: %s", err)
        if not settings.debug:
            raise ConfigurationError(
                f"Fatal configuration errors ({len(config_errors)}): "
                + "; ".join(config_errors)
            )
        else:
            logger.warning(
                "Continuing in debug mode despite %d config warning(s)",
                len(config_errors),
            )

    # Connect to PostgreSQL
    await init_async_engine()

    # Run Alembic migrations to keep schema up-to-date, then ensure tables
    from app.database import get_async_engine, Base
    import app.models.auth  # noqa: F401
    try:
        from alembic.config import Config as AlembicConfig
        from alembic import command as alembic_command
        import os
        alembic_cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "alembic.ini"))
        alembic_cfg.set_main_option("sqlalchemy.url", settings.database_url.replace("+asyncpg", ""))
        alembic_cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "alembic"))
        alembic_command.upgrade(alembic_cfg, "head")
        logger.info("Alembic migrations applied")
    except Exception as mig_exc:
        logger.warning("Alembic migration skipped (non-fatal): %s", mig_exc)

    # Ensure all tables exist (fallback for first-run without migrations)
    engine = get_async_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables ensured")

    # Start WebSocket ↔ Redis bridge
    await ws_manager.start_redis_listener()

    # Start Market Data Service (Phase 2)
    mds = None
    redis_client = None
    try:
        import redis.asyncio as aioredis
        from app.services.market_data import MarketDataService
        redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
        session_factory = get_async_session_factory()
        mds = MarketDataService(
            db_session_factory=session_factory,
            redis=redis_client,
            settings=settings,
        )
        await mds.start()
        logger.info("MarketDataService started")
    except Exception:
        logger.error("Failed to start MarketDataService", exc_info=True)

    logger.info("NexusTrader API ready")
    yield

    # Shutdown
    logger.info("Shutting down NexusTrader API service")
    if mds is not None:
        try:
            await mds.stop()
        except Exception:
            logger.error("Error stopping MarketDataService", exc_info=True)
    if redis_client is not None:
        try:
            await redis_client.aclose()
        except Exception:
            logger.error("Error closing MDS Redis client", exc_info=True)
    await ws_manager.stop_redis_listener()
    await dispose_async_engine()


# ── App Factory ─────────────────────────────────────────────
settings = get_settings()

app = FastAPI(
    title="NexusTrader API",
    version="1.0.0",
    description="NexusTrader Web — Trading Engine API",
    lifespan=lifespan,
    docs_url="/docs" if settings.debug else None,
    redoc_url="/redoc" if settings.debug else None,
)

# ── Rate Limiting ──────────────────────────────────────────
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)


# ── Global Exception Handlers (Phase 6A) ──────────────────

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Return normalized 422 with field-level errors."""
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
    errors = []
    for err in exc.errors():
        errors.append({
            "field": ".".join(str(loc) for loc in err.get("loc", [])),
            "message": err.get("msg", ""),
            "type": err.get("type", ""),
        })
    logger.warning(
        "Validation error on %s %s: %d field(s)",
        request.method, request.url.path, len(errors),
    )
    return JSONResponse(
        status_code=422,
        content={
            "detail": "Validation error",
            "errors": errors,
            "request_id": request_id,
        },
        headers={"X-Request-ID": request_id},
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """
    Catch-all for unhandled exceptions that escape through the routing
    layer (but not through middleware). The AuditMiddleware also catches
    exceptions as a safety net for middleware-level errors.
    """
    import traceback
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
    logger.error(
        "Unhandled exception on %s %s: %s",
        request.method, request.url.path, exc,
        exc_info=True,
    )
    is_debug = os.getenv("NEXUS_DEBUG", "false").lower() == "true"
    content: dict = {
        "detail": "Internal server error",
        "request_id": request_id,
    }
    if is_debug:
        content["traceback"] = traceback.format_exception(type(exc), exc, exc.__traceback__)
    return JSONResponse(
        status_code=500,
        content=content,
        headers={"X-Request-ID": request_id},
    )


# ── Middleware Stack ────────────────────────────────────────
# Starlette executes middleware in REVERSE order of add_middleware,
# so the LAST added runs FIRST on incoming requests.
# Order: CF Access → Security Headers → CORS → Rate Limit → per-route JWT
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(SecurityHeadersMiddleware)  # add security headers to responses
app.add_middleware(AuditMiddleware)             # request audit logging + X-Request-ID
app.add_middleware(CloudflareAccessMiddleware)  # outermost: runs first

# ── Routers ─────────────────────────────────────────────────
app.include_router(health_router)
app.include_router(auth_router, prefix="/api/v1")
app.include_router(engine_router, prefix="/api/v1")
app.include_router(dashboard_router, prefix="/api/v1")
app.include_router(scanner_router, prefix="/api/v1")
app.include_router(signals_router, prefix="/api/v1")
app.include_router(risk_router, prefix="/api/v1")
app.include_router(trades_router, prefix="/api/v1")
app.include_router(system_router, prefix="/api/v1")
app.include_router(settings_router, prefix="/api/v1")
app.include_router(charts_router, prefix="/api/v1")
app.include_router(trading_router, prefix="/api/v1")
app.include_router(logs_router, prefix="/api/v1")
app.include_router(analytics_router, prefix="/api/v1")
app.include_router(backtest_router, prefix="/api/v1")
app.include_router(validation_router, prefix="/api/v1")
app.include_router(vault_router, prefix="/api/v1")
app.include_router(exchanges_router, prefix="/api/v1")
app.include_router(monitor_router, prefix="/api/v1")
app.include_router(market_data_router, prefix="/api/v1")
app.include_router(ws_router)


@app.get("/")
async def root():
    return {
        "service": "nexus-api",
        "version": "1.0.0",
        "docs": "/docs" if settings.debug else "disabled",
    }
