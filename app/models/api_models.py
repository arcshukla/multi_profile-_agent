"""
api_models.py
-------------
Pydantic models for API request/response bodies (chat, indexing, logs).
"""

from __future__ import annotations
from typing import Optional, Any
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

class ChatMessage(BaseModel):
    role: str          # "user" | "assistant" | "system"
    content: str


class ChatRequest(BaseModel):
    message: str
    history: list[ChatMessage] = []
    session_id: str = ""


class TokenUsage(BaseModel):
    """Token consumption for a single chat turn (all LLM calls combined)."""
    prompt_tokens:     int = 0
    completion_tokens: int = 0
    total_tokens:      int = 0
    call_count:        int = 0   # number of separate LLM API calls in this turn


class ChatResponse(BaseModel):
    answer:          str
    followups:       list[str]  = []
    session_id:      str        = ""
    tokens_used:     TokenUsage = TokenUsage()
    history_trimmed: bool       = False   # True when earlier messages were dropped per profile limit
    warming_up:      bool       = False   # True when the profile index is still building after restart


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------

class IndexRequest(BaseModel):
    force: bool = False


class IndexStatusResponse(BaseModel):
    slug: str
    status: str          # "not_indexed" | "indexed" | "indexing" | "failed"
    chunk_count: int = 0
    document_count: int = 0
    last_indexed: Optional[str] = None


class IndexHistoryEntry(BaseModel):
    timestamp: str
    profile_slug: str
    status: str
    document_count: int = 0
    duration_seconds: float = 0.0
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

class PromptEntry(BaseModel):
    name: str
    short_name: str
    content: str


class PromptsResponse(BaseModel):
    prompts: dict[str, PromptEntry]
    is_default: bool = False


class UpdatePromptRequest(BaseModel):
    short_name: str
    content: str


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

class DocumentInfo(BaseModel):
    filename: str
    size_bytes: int
    uploaded_at: Optional[str] = None


class DocumentListResponse(BaseModel):
    slug: str
    documents: list[DocumentInfo]


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

class LogEntry(BaseModel):
    line: str


class LogsResponse(BaseModel):
    slug: Optional[str]
    log_type: str   # "app" | "indexing" | "chat" | "profile"
    lines: list[str]
    total_lines: int


# ---------------------------------------------------------------------------
# Generic
# ---------------------------------------------------------------------------

class SuccessResponse(BaseModel):
    success: bool = True
    message: str = "OK"


class ErrorResponse(BaseModel):
    success: bool = False
    error: str
    detail: Optional[Any] = None
