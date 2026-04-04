"""
user_models.py
--------------
Canonical in-memory representation of a profile owner.

Everyone in system/users.json owns exactly one profile.
Admins are resolved from settings.ADMIN_EMAILS at login time — they need
no entry in this file (though an admin may also own a profile, in which
case they do have an entry here).

Field notes:
  - email      : Google login email — primary key in users.json
  - slug        : profile slug, always present for owners
  - name        : display name — single source of truth (shown in UI and emails)
  - status      : enabled / disabled / suspended / soft_deleted
  - created_at  : ISO timestamp of when the record was created
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class UserEntity(BaseModel):
    """Profile owner record. Role is never stored — derived at login from ADMIN_EMAILS."""

    email: str
    slug: str
    name: str = ""
    status: str = "enabled"
    created_at: Optional[str] = None
