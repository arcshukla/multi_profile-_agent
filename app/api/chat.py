"""
api/chat.py  —  Chat endpoints

Rate limiting: 20 requests / minute per IP on the POST /chat endpoint.
Configured via settings.CHAT_RATE_LIMIT (default "20/minute").
"""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request

from app.core.constants import STATUS_ENABLED, STATUS_DISABLED, STATUS_SUSPENDED, STATUS_SOFT_DELETED, STATUS_DELETED
from app.core.logging_config import get_logger
from app.models.api_models import ChatRequest, ChatResponse
from app.models.user_models import UserEntity
from app.services.chat_service import chat_service
from app.services.profile_service import profile_service
from app.services.prompt_service import prompt_service

logger = get_logger(__name__)

router = APIRouter(prefix="/api/profiles/{slug}/chat", tags=["chat"])

# ── Rate limiter ──────────────────────────────────────────────────────────────

try:
    from slowapi import Limiter
    from slowapi.util import get_remote_address
    _limiter = Limiter(key_func=get_remote_address)
    _RATE_LIMIT_AVAILABLE = True
except ImportError:
    _limiter = None
    _RATE_LIMIT_AVAILABLE = False
    logger.warning("slowapi not installed — chat rate limiting is DISABLED")


# ── Shared dependency ──────────────────────────────────────────────────────────

def _require_enabled_profile(slug: str) -> UserEntity:
    """FastAPI dependency: resolve slug → enabled UserEntity or raise HTTP error."""
    entry = profile_service.get_entry(slug)
    if not entry:
        raise HTTPException(404, "This profile does not exist. If you are a professional looking to create your own AI-powered career page, visit the platform to get started.")
    if entry.status == STATUS_DISABLED:
        raise HTTPException(503, "This profile is currently disabled.")
    if entry.status == STATUS_SUSPENDED:
        raise HTTPException(403, "This profile is not available.")
    if entry.status in (STATUS_SOFT_DELETED, STATUS_DELETED):
        raise HTTPException(410, "This profile is no longer available.")
    if entry.status != STATUS_ENABLED:
        raise HTTPException(403, f"Profile '{slug}' is not active")
    return entry


# ── Endpoints ──────────────────────────────────────────────────────────────────

def _chat_handler(
    request:          Request,
    slug:             str,
    req:              ChatRequest,
    background_tasks: BackgroundTasks,
    _entry:           UserEntity,
) -> ChatResponse:
    logger.info("Chat request | slug=%s | len=%d", slug, len(req.message))
    resp = chat_service.chat(
        slug             = slug,
        message          = req.message,
        history          = req.history,
        session_id       = req.session_id,
        background_tasks = background_tasks,
    )
    logger.info("Chat response | slug=%s | tokens=%d calls=%d | followups=%d",
                slug, resp.tokens_used.total_tokens,
                resp.tokens_used.call_count, len(resp.followups))
    return resp


if _RATE_LIMIT_AVAILABLE:
    from app.core.config import settings as _settings

    @router.post("", response_model=ChatResponse)
    @_limiter.limit(getattr(_settings, "CHAT_RATE_LIMIT", "20/minute"))
    def chat(
        request:          Request,
        slug:             str,
        req:              ChatRequest,
        background_tasks: BackgroundTasks,
        _entry:           UserEntity = Depends(_require_enabled_profile),
    ) -> ChatResponse:
        return _chat_handler(request, slug, req, background_tasks, _entry)
else:
    @router.post("", response_model=ChatResponse)
    def chat(
        request:          Request,
        slug:             str,
        req:              ChatRequest,
        background_tasks: BackgroundTasks,
        _entry:           UserEntity = Depends(_require_enabled_profile),
    ) -> ChatResponse:
        return _chat_handler(request, slug, req, background_tasks, _entry)


@router.get("/welcome")
def get_welcome(slug: str):
    if not profile_service.profile_exists(slug):
        raise HTTPException(404, f"Profile '{slug}' not found")
    logger.debug("Welcome request | slug=%s", slug)
    return {
        "welcome":     chat_service.get_welcome_message(slug),
        "followups":   chat_service.get_initial_followups(slug),
        "placeholder": prompt_service.chat_placeholder(slug),
    }
