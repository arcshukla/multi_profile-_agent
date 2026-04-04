"""
profile_models.py
-----------------
Pydantic models for profile API layer.
Used for validation in API routes and service layers.

Owner registry data lives in UserEntity (app.models.user_models).
"""

from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Request / Response models  (API layer)
# ---------------------------------------------------------------------------

class CreateProfileRequest(BaseModel):
    """Payload for POST /api/profiles."""
    name: str = Field(..., min_length=2, max_length=120)
    owner_email: str = Field(..., description="Owner's Google login email")
    status: str = "enabled"


class UpdateProfileRequest(BaseModel):
    """Payload for PATCH /api/profiles/{slug}."""
    name: Optional[str] = None
    status: Optional[str] = None


class ProfileResponse(BaseModel):
    """API response shape for a profile."""
    name: str
    slug: str
    status: str
    base_folder: str
    has_photo: bool = False
    photo_ts: int = 0          # file modification timestamp for cache-busting
    document_count: int = 0
    chunk_count: int = 0
    last_indexed: Optional[str] = None   # ISO timestamp or None
    created_at: Optional[str] = None     # ISO timestamp of owner record creation


class ProfileListResponse(BaseModel):
    """Paginated list of profiles."""
    profiles: list[ProfileResponse]
    total: int
    page: int
    page_size: int
