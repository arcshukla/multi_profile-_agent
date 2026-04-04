"""
main.py
-------
FastAPI application factory and startup/shutdown hooks.

Run locally:
  uvicorn app.main:app --reload --host 0.0.0.0 --port 7860
Push for HF Space:
  git push space master:main --force

Route modules:
  app.api.auth_routes   — login, OAuth callback, logout, register, explore, /
  app.api.admin_routes  — all /admin/* page routes and HTMX partials
  app.api.owner         — /owner/* portal
  app.api.profiles      — REST /api/profiles/*
  app.api.chat          — REST /api/profiles/{slug}/chat
  app.api.documents     — REST /api/profiles/{slug}/documents/*
  app.api.indexing      — REST /api/profiles/{slug}/index/*
  app.api.prompts       — REST /api/profiles/{slug}/prompts/*
  app.api.logs          — REST /api/logs/*
  app.api.billing       — /owner/billing, /api/billing/*
"""

import uuid
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import RedirectResponse as _RedirectResponse

from app.core.config import settings, STATIC_DIR, TEMPLATES_DIR
from app.core.logging_config import get_logger, set_current_session_id
from app.core.constants import ROLE_ADMIN

from app.api.auth_routes  import router as auth_router
from app.api.admin_routes import router as admin_router
from app.api.owner        import router as owner_router
from app.api.profiles     import router as profiles_router
from app.api.chat         import router as chat_router
from app.api.documents    import router as documents_router
from app.api.indexing     import router as indexing_router, history_router
from app.api.prompts      import router as prompts_router
from app.api.logs         import router as logs_router
from app.api.billing      import router as billing_router

logger = get_logger(__name__)


# =============================================================================
# MIDDLEWARE
# =============================================================================

class AdminAuthMiddleware(BaseHTTPMiddleware):
    """
    Guards all /admin/* routes.
    Requires role=admin
    """
    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith("/admin"):
            user = request.session.get("user")
            if not user or user.get("role") != ROLE_ADMIN:
                return _RedirectResponse(url="/login", status_code=302)
        return await call_next(request)


class ActorContextMiddleware(BaseHTTPMiddleware):
    """
    Stamps every log record in this request with actor + short request ID.
    Format: "{email}#{req_id}" for authenticated users, "anon#{req_id}" otherwise.
    This lets you correlate all log lines from one request even under concurrency.
    """
    async def dispatch(self, request: Request, call_next):
        req_id = uuid.uuid4().hex[:8]
        user   = request.session.get("user")
        actor  = user["email"] if user and user.get("email") else "anon"
        set_current_session_id(f"{actor}#{req_id}")
        return await call_next(request)


class CanonicalHostMiddleware(BaseHTTPMiddleware):
    """Keep local development on one host so session cookies stay usable."""

    async def dispatch(self, request: Request, call_next):
        if settings.IS_LOCAL:
            canonical = urlsplit(settings.APP_URL)
            request_host = request.url.hostname or ""
            canonical_host = canonical.hostname or ""
            if canonical_host and request_host and request_host != canonical_host:
                query = f"?{request.url.query}" if request.url.query else ""
                target = f"{settings.APP_URL.rstrip('/')}{request.url.path}{query}"
                return _RedirectResponse(url=target, status_code=307)
        return await call_next(request)


_PORTAL_PREFIXES: tuple[tuple[str, str], ...] = (
    ("/admin",      "admin"),
    ("/dashboard",  "owner"),
    ("/chat/",      "chat"),
    ("/explore",    "explore"),
)


class BrowserContextMiddleware(BaseHTTPMiddleware):
    """
    Logs browser / locale context on top-level portal page loads.

    Fires only on GET requests that are NOT HTMX partial fetches, matching
    the four portal entry points: admin, owner, chat, explore.

    Logged fields (land in app.log, correlated with the actor already stamped
    by ActorContextMiddleware):
      portal         — admin | owner | chat | explore
      ua             — full User-Agent string
      lang           — Accept-Language header (first preference = likely locale)
    """

    async def dispatch(self, request: Request, call_next):
        if (
            request.method == "GET"
            and "HX-Request" not in request.headers
        ):
            path = request.url.path
            for prefix, portal in _PORTAL_PREFIXES:
                if path.startswith(prefix):
                    logger.info(
                        "PORTAL_VISIT | portal=%s | ua=%s | lang=%s",
                        portal,
                        request.headers.get("user-agent", "—"),
                        request.headers.get("accept-language", "—"),
                    )
                    break
        return await call_next(request)


# =============================================================================
# APPLICATION FACTORY
# =============================================================================

app = FastAPI(title="AI Profile Platform", version=settings.APP_VERSION)

# ── Rate limiter (slowapi) ────────────────────────────────────────────────────
try:
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded
    from slowapi.util import get_remote_address
    _limiter = Limiter(key_func=get_remote_address)
    app.state.limiter = _limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    logger.info("Chat rate limiting enabled (slowapi)")
except ImportError:
    logger.warning("slowapi not installed — chat rate limiting is DISABLED")

# ── Static files ──────────────────────────────────────────────────────────────
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ── Global Jinja2 templates (used by shared helpers) ─────────────────────────
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["support_email"] = settings.SUPPORT_EMAIL

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(owner_router)
app.include_router(profiles_router)
app.include_router(chat_router)
app.include_router(documents_router)
app.include_router(indexing_router)
app.include_router(history_router)
app.include_router(prompts_router)
app.include_router(logs_router)
app.include_router(billing_router)

# ── Middleware (last added = outermost = runs first) ──────────────────────────
app.add_middleware(AdminAuthMiddleware)       # innermost  — runs fourth
app.add_middleware(BrowserContextMiddleware) # runs third — actor already set
app.add_middleware(ActorContextMiddleware)   # runs second (session already populated)
app.add_middleware(CanonicalHostMiddleware)

# CSRF protection — exempts GET/HEAD/OPTIONS and /api/* (JSON APIs use no cookies)
try:
    import re as _re_csrf
    from starlette_csrf import CSRFMiddleware as _CSRFMiddleware
    app.add_middleware(
        _CSRFMiddleware,
        secret=settings.SESSION_SECRET_KEY,
        # Exempt JSON API routes (use Bearer tokens / no cookies) and OAuth callback
        exempt_urls=[
            _re_csrf.compile(r"^/api/"),
            _re_csrf.compile(r"^/auth/"),
            _re_csrf.compile(r"^/health$"),
        ],
    )
    logger.info("CSRF protection enabled (starlette-csrf)")
except ImportError:
    logger.warning("starlette-csrf not installed — CSRF protection is DISABLED")

app.add_middleware(                           # outermost  — runs first, populates session
    SessionMiddleware,
    secret_key=settings.SESSION_SECRET_KEY,
    https_only=not settings.IS_LOCAL,        # True in production (HTTPS), False in local dev
    max_age=60 * 60 * 6,                     # 6-hour session
)


# =============================================================================
# LIVENESS PROBE
# =============================================================================

@app.get("/health", include_in_schema=False)
def health():
    """Lightweight liveness probe for HF Spaces and load-balancers."""
    return {"status": "ok", "version": settings.APP_VERSION}


# =============================================================================
# STARTUP / SHUTDOWN
# =============================================================================

@app.on_event("startup")
async def startup_event():
    import asyncio
    from app.storage.hf_sync import hf_sync
    from app.services.profile_service import profile_service
    # Pull persistent data from HF Dataset (HF Spaces only).
    # Runs in a thread pool so the async event loop is not blocked.
    # On local dev hf_sync.pull() is a no-op — this line is harmless.
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, hf_sync.pull)

    # Start periodic log sync (HF Spaces only)
    hf_sync.start_log_sync_loop(settings.HF_LOG_SYNC_INTERVAL_MINUTES)

    enabled = [p for p in profile_service.list_profiles() if p.status == "enabled"]

    logger.info("=" * 60)
    logger.info("AI Profile Platform starting up")
    logger.info("Model: %s", settings.AI_MODEL)
    logger.info("Enabled profiles: %d", len(enabled))
    logger.info("Indexes will build on-demand when visitors first arrive")
    logger.info("=" * 60)


@app.on_event("shutdown")
async def shutdown_event():
    logger.info("AI Profile Platform shutting down")
    from app.storage.hf_sync import hf_sync
    hf_sync.push_logs()   # final log flush to HF Dataset before process exits
