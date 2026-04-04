"""
user_service.py
---------------
Manages the profile owner registry: system/users.json.

Schema (one entry per profile owner, keyed by login email):
  {
    "owner@example.com": {
      "slug":       "profile-slug",
      "name":       "Display Name",
      "status":     "enabled" | "disabled" | "deleted",
      "created_at": "2026-03-28T10:00:00Z"
    }
  }

Auth notes:
  - Everyone in this file is a profile owner (role=owner in session).
  - Admins are identified solely via settings.ADMIN_EMAILS — no file entry needed.
  - An admin who also owns a profile will have an entry here (slug is set in their session).

Crash recovery:
  - _save() rotates up to DATA_BACKUP_COUNT (default 3) backup files before writing.
  - _load() automatically falls back to backups if the primary file is corrupt/missing.
"""

import json
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.core.config import settings, SYSTEM_DIR
from app.core.logging_config import get_logger
from app.models.user_models import UserEntity

logger = get_logger(__name__)

_USERS_FILE = SYSTEM_DIR / "users.json"


class UserService:
    """Thread-safe CRUD for the profile owner registry."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: Optional[dict] = None           # email → record, None = not loaded
        self._slug_index: dict[str, str] = {}         # slug → email reverse index

    # ── Read ──────────────────────────────────────────────────────────────────

    def _load(self) -> dict:
        """
        Load users.json, falling back to numbered backups on failure.
        Result is cached in memory and served from cache on subsequent calls.
        Call _invalidate_cache() before any write to ensure consistency.
        """
        if self._cache is not None:
            return self._cache

        max_bak = getattr(settings, "DATA_BACKUP_COUNT", 3)
        candidates = [_USERS_FILE] + [
            _USERS_FILE.parent / f"users.bak{i}.json" for i in range(1, max_bak + 1)
        ]
        data: dict = {}
        for path in candidates:
            try:
                if path.exists():
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if path != _USERS_FILE:
                        logger.warning(
                            "users.json unreadable — loaded from backup: %s", path.name
                        )
                    break
            except Exception as e:
                logger.error("Failed to load %s: %s", path.name, e)

        self._cache = data
        self._slug_index = {
            rec["slug"]: email
            for email, rec in data.items()
            if rec.get("slug")
        }
        return self._cache

    def _invalidate_cache(self) -> None:
        """Discard the in-memory cache so the next read reloads from disk."""
        self._cache = None
        self._slug_index = {}

    def _save(self, data: dict) -> None:
        """Rotate backups then write. Keeps up to DATA_BACKUP_COUNT backups."""
        from app.storage.hf_sync import hf_sync

        self._invalidate_cache()   # invalidate before write so stale data is never served
        max_bak = getattr(settings, "DATA_BACKUP_COUNT", 3)
        # Shift existing backups: bak2→bak3, bak1→bak2
        for i in range(max_bak - 1, 0, -1):
            src = _USERS_FILE.parent / f"users.bak{i}.json"
            dst = _USERS_FILE.parent / f"users.bak{i + 1}.json"
            if src.exists():
                shutil.copy2(src, dst)
        # Copy current file to bak1
        if _USERS_FILE.exists():
            shutil.copy2(_USERS_FILE, _USERS_FILE.parent / "users.bak1.json")
        _USERS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        hf_sync.push_file(_USERS_FILE)

    def list_users(self) -> dict[str, dict]:
        """Return raw dict (email → record) for the admin users table."""
        with self._lock:
            return dict(self._load())

    def list_owners(self) -> list[UserEntity]:
        """Return all owner records as UserEntity objects."""
        with self._lock:
            data = self._load()
        return [UserEntity(email=email, **rec) for email, rec in data.items()]

    def get_user(self, email: str) -> Optional[UserEntity]:
        """Return owner record by email, or None."""
        email = email.lower().strip()
        with self._lock:
            rec = self._load().get(email)
        return UserEntity(email=email, **rec) if rec else None

    def get_user_by_slug(self, slug: str) -> Optional[UserEntity]:
        """
        Return owner record by profile slug, or None.
        Uses the in-memory reverse index — O(1) instead of O(n) scan.
        """
        with self._lock:
            data  = self._load()          # populates _slug_index as side-effect
            email = self._slug_index.get(slug)
            if not email:
                return None
            rec = data.get(email)
            return UserEntity(email=email, **rec) if rec else None

    # ── Resolve session (called after Google OAuth callback) ──────────────────

    def resolve_session(self, email: str, google_name: str) -> Optional[dict]:
        """
        Build the session payload for a newly authenticated Google user.

        Lookup order:
          1. ADMIN_EMAILS env var  → role=admin (slug from users.json if they also own a profile)
          2. users.json            → role=owner
          3. Neither               → None (access denied)

        Returns {"email", "name", "role", "slug"} or None.
        """
        email = email.lower().strip()
        is_admin = email in [e.lower() for e in settings.ADMIN_EMAILS]
        record = self.get_user(email)

        if is_admin:
            return {
                "email": email,
                "name":  (record.name if record else None) or google_name,
                "role":  "admin",
                "slug":  record.slug if record else None,
            }
        if record:
            return {
                "email": email,
                "name":  record.name or google_name,
                "role":  "owner",
                "slug":  record.slug,
            }

        logger.warning("Login attempt from unregistered email: %s", email)
        return None

    # ── Write ─────────────────────────────────────────────────────────────────

    def add_user(
        self,
        email: str,
        name: str,
        slug: str,
        status: str = "enabled",
    ) -> tuple[bool, str]:
        """
        Add or update a profile owner record.

        Returns (success, error_message).
        Enforces: one owner per slug.
        """
        email = email.lower().strip()
        if not email:
            return False, "Email is required."
        if not slug:
            return False, "Slug is required."

        with self._lock:
            data = self._load()
            # Enforce email uniqueness — an email can only own one profile
            if email in data and data[email].get("slug") != slug:
                existing_slug = data[email].get("slug")
                return False, f"'{email}' is already registered as owner of '{existing_slug}'."
            # Enforce slug uniqueness — a profile can only have one owner
            for existing_email, rec in data.items():
                if rec.get("slug") == slug and existing_email != email:
                    return False, f"Profile '{slug}' is already assigned to {existing_email}."
            data[email] = {
                "slug":       slug,
                "name":       name,
                "status":     status,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            self._save(data)

        logger.info("Added owner %s → slug=%s status=%s", email, slug, status)
        return True, ""

    def update_email(self, old_email: str, new_email: str) -> tuple[bool, str]:
        """Rename a owner's login email. Slug and name unchanged."""
        old_email = old_email.lower().strip()
        new_email = new_email.lower().strip()
        if not new_email:
            return False, "Email is required."

        with self._lock:
            data = self._load()
            if old_email not in data:
                return False, f"User '{old_email}' not found."
            if new_email != old_email and new_email in data:
                return False, f"Email '{new_email}' is already in use."
            data[new_email] = data.pop(old_email)
            self._save(data)

        logger.info("Updated email %s → %s", old_email, new_email)
        return True, ""

    def update_name(self, email: str, name: str) -> tuple[bool, str]:
        """Update a owner's display name."""
        email = email.lower().strip()
        with self._lock:
            data = self._load()
            if email not in data:
                return False, f"User '{email}' not found."
            data[email]["name"] = name
            self._save(data)
        logger.info("Updated name for %s → %s", email, name)
        return True, ""

    def update_status(self, slug: str, status: str) -> tuple[bool, str]:
        """Update the status of a profile owner record by slug."""
        with self._lock:
            data = self._load()
            for email, rec in data.items():
                if rec.get("slug") == slug:
                    rec["status"] = status
                    self._save(data)
                    logger.info("Status updated slug=%s → %s", slug, status)
                    return True, ""
        return False, f"Profile '{slug}' not found."

    def remove_user(self, email: str) -> bool:
        """Remove an owner by email. Returns True if removed."""
        email = email.lower().strip()
        with self._lock:
            data = self._load()
            if email not in data:
                return False
            del data[email]
            self._save(data)
        logger.info("Removed owner %s", email)
        return True

    def remove_user_by_slug(self, slug: str) -> bool:
        """Remove an owner by profile slug. Returns True if removed."""
        with self._lock:
            data = self._load()
            for email, rec in list(data.items()):
                if rec.get("slug") == slug:
                    del data[email]
                    self._save(data)
                    logger.info("Removed owner slug=%s (%s)", slug, email)
                    return True
        return False


user_service = UserService()
